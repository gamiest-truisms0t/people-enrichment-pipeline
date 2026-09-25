#!/bin/bash
# Phase 0 tooling setup for macOS on Apple silicon. Safe to re-run.
# Installs: Homebrew, terraform (HashiCorp tap), awscli, gh, tflint, gitleaks,
#           uv, Python 3.13 (via uv), checkov and pre-commit (as uv tools).
set -u

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# 1. Homebrew — the only step that needs your password (sudo, once).
if [ ! -x /opt/homebrew/bin/brew ]; then
  step "Installing Homebrew: press RETURN when asked, then enter your macOS password"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if [ -x /opt/homebrew/bin/brew ]; then
  if ! grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null; then
    step "Adding Homebrew to PATH in ~/.zprofile"
    printf '\neval "$(/opt/homebrew/bin/brew shellenv)"\n' >> "$HOME/.zprofile"
  fi
  eval "$(/opt/homebrew/bin/brew shellenv)"
  step "Installing awscli, gh, gitleaks and terraform (hashicorp/tap)"
  brew install awscli gh gitleaks hashicorp/tap/terraform
else
  echo "Homebrew is not available; skipping the brew packages." >&2
fi

# 1b. tflint is no longer in homebrew-core: install the official release binary.
if [ ! -x "$HOME/.local/bin/tflint" ]; then
  step "Installing tflint from its GitHub release (darwin_arm64, checksum verified)"
  mkdir -p "$HOME/.local/bin"
  tmp="$(mktemp -d)"
  ver="$(curl -fsSL https://api.github.com/repos/terraform-linters/tflint/releases/latest | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')"
  curl -fsSL -o "$tmp/tflint_darwin_arm64.zip" "https://github.com/terraform-linters/tflint/releases/download/$ver/tflint_darwin_arm64.zip"
  curl -fsSL -o "$tmp/checksums.txt" "https://github.com/terraform-linters/tflint/releases/download/$ver/checksums.txt"
  (cd "$tmp" && grep ' tflint_darwin_arm64.zip$' checksums.txt | shasum -a 256 -c -) \
    && unzip -oq "$tmp/tflint_darwin_arm64.zip" -d "$HOME/.local/bin" \
    && chmod +x "$HOME/.local/bin/tflint"
  rm -rf "$tmp"
fi

# 2. uv (no sudo needed), Python 3.13, and the Python-based tools.
if [ ! -x "$HOME/.local/bin/uv" ] && ! command -v uv >/dev/null 2>&1; then
  step "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

step "Installing Python 3.13 with uv"
uv python install 3.13

step "Installing checkov and pre-commit as uv tools"
uv tool install checkov
uv tool install pre-commit

# 3. Summary
step "Installed versions"
for c in brew terraform aws gh tflint gitleaks uv checkov pre-commit; do
  if command -v "$c" >/dev/null 2>&1; then
    printf '%-11s %s\n' "$c" "$("$c" --version 2>&1 | head -1)"
  else
    printf '%-11s MISSING\n' "$c"
  fi
done
uv python list --only-installed 2>/dev/null | head -5
echo
echo "Done. Open a new terminal tab (or run: source ~/.zprofile) so PATH picks up Homebrew and uv."
