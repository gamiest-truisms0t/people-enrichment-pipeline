# GitHub Actions deploys through OIDC: no long-lived AWS keys in GitHub. Three roles with
# the least privilege each workflow needs, all trusting only this repository:
#
#   github-plan      pull requests   reads the dev state to render `terraform plan
#                                    -refresh=false`; cannot read or touch any resource
#   github-readonly  main (schedule) full read access for drift detection (a real refresh),
#                                    plus decrypting the SSM parameter Terraform manages
#   github-apply     main (push)     PowerUser plus IAM management scoped to this project's
#                                    roles, for `terraform apply` after a merge
#
# Lives in the bootstrap stack because it is account-level and must exist before CI can.

data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's root CA thumbprints; AWS now validates GitHub's OIDC issuer directly, but the
  # provider resource still requires the list.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

locals {
  github_subjects = {
    plan     = ["repo:${var.github_repository}:pull_request"]
    readonly = ["repo:${var.github_repository}:ref:refs/heads/main"]
    apply    = ["repo:${var.github_repository}:ref:refs/heads/main"]
  }
  state_object_arns = [for env in var.environments : "${aws_s3_bucket.state.arn}/${env}/terraform.tfstate"]
  project_role_arns = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project}-*"]
}

data "aws_iam_policy_document" "github_trust" {
  for_each = local.github_subjects

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = each.value
    }
  }
}

resource "aws_iam_role" "github" {
  for_each = local.github_subjects

  name                 = "${var.project}-github-${each.key}"
  description          = "GitHub Actions (${var.github_repository}) ${each.key} role, assumed through OIDC"
  assume_role_policy   = data.aws_iam_policy_document.github_trust[each.key].json
  max_session_duration = 3600
}

# ------------------------------------------------------------------ plan (pull requests)

data "aws_iam_policy_document" "github_plan" {
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }

  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = local.state_object_arns
  }

  # The stack's data sources: the aws/ssm alias lookup. Caller identity needs no permission.
  statement {
    sid       = "ResolveKmsAlias"
    actions   = ["kms:ListAliases"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_plan" {
  name   = "plan"
  role   = aws_iam_role.github["plan"].id
  policy = data.aws_iam_policy_document.github_plan.json
}

# ------------------------------------------------------------------ readonly (drift)

resource "aws_iam_role_policy_attachment" "github_readonly" {
  role       = aws_iam_role.github["readonly"].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "github_readonly_extra" {
  # Terraform reads the SecureString parameter it manages (decrypted) on every refresh.
  statement {
    sid       = "DecryptSsmParameters"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm.target_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_readonly_extra" {
  name   = "drift-extras"
  role   = aws_iam_role.github["readonly"].id
  policy = data.aws_iam_policy_document.github_readonly_extra.json
}

# ------------------------------------------------------------------ apply (main)

resource "aws_iam_role_policy_attachment" "github_apply_poweruser" {
  role       = aws_iam_role.github["apply"].name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

data "aws_iam_policy_document" "github_apply_iam" {
  # PowerUserAccess excludes IAM; the stack manages roles and inline policies named after
  # the project, and passes them to Lambda, Step Functions and EventBridge.
  statement {
    sid = "ManageProjectRoles"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:ListRoleTags",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = local.project_role_arns
  }

  statement {
    sid       = "PassProjectRoles"
    actions   = ["iam:PassRole"]
    resources = local.project_role_arns

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com", "states.amazonaws.com", "events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_apply_iam" {
  name   = "project-iam"
  role   = aws_iam_role.github["apply"].id
  policy = data.aws_iam_policy_document.github_apply_iam.json
}
