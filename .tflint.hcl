# tflint configuration. The AWS ruleset plugin is added in Phase 2 once infra/ has code.
config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
