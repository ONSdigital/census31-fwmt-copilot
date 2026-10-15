# FWMT workspace bootstrap

The workspace bootstrap script copies `workspace.json` into a new directory and checks out the documents repository and each listed repository at its configured ref. It needs Python 3, Git, network access to the remotes, and a destination that does not already exist. It checks Git and repository access before creating the destination.

## Run locally

From the project root (the directory containing `workspace.json`):

```sh
python3 dev-workspace/scripts/create_workspace.py --workspace /path/to/new-workspace --config workspace.json
```

For private GitHub repositories, your existing Git credential helper can authenticate. Alternatively, [create a fine-grained personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token), put it in a readable file outside the repository, restrict its permissions to the intended user, and run:

```sh
export GITHUB_TOKEN_FILE=/path/to/github-token-file
export GIT_ASKPASS="$PWD/dev-workspace/scripts/git_askpass.py"
export GIT_TERMINAL_PROMPT=0
python3 dev-workspace/scripts/create_workspace.py --workspace /path/to/new-workspace --config workspace.json
```

The askpass helper only answers GitHub HTTPS prompts. Keep these variables set for later `git push` commands, or use your normal local Git credential helper. A configured credential helper may take precedence over askpass.

## Run in a container

Build the image from the project root so `workspace.json` remains in the build context. No token is required at build time:

```sh
docker build -f dev-workspace/Dockerfile -t dev-workspace .
```

Mount a writable directory for the output. For private repositories, mount the token file read-only at runtime and point `GITHUB_TOKEN_FILE` at it:

```sh
docker run --rm -it \
  --mount type=bind,src=/path/to/writable-parent,dst=/work \
  --mount type=bind,src=/path/to/github-token-file,dst=/run/secrets/github_token,readonly \
  -e GITHUB_TOKEN_FILE=/run/secrets/github_token \
  -e GIT_CONFIG_COUNT=2 \
  -e GIT_CONFIG_KEY_0=user.name \
  -e GIT_CONFIG_VALUE_0="Your Name" \
  -e GIT_CONFIG_KEY_1=user.email \
  -e GIT_CONFIG_VALUE_1="you@example.com" \
  dev-workspace \
  python3 scripts/create_workspace.py --workspace /work/fwmt --config /app/workspace.json
```

The image already sets `GIT_ASKPASS` and disables interactive Git prompts. Pass the identity settings and mount the token file again when running a later container session that will commit and push. Public repositories do not require a token. If you run the container as a non-root user, ensure that user can read the mounted token and write to the output directory. Do not put tokens in the manifest, remote URL, Dockerfile, build arguments, or image layers.

## Commit and push

To create commits locally, configure `user.name` and `user.email`; the container example above passes these settings at runtime instead:

```sh
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

For pushes to private GitHub repositories, use a fine-grained token authorized for the target repositories with **Contents: read and write** permission. Organization access or SSO authorization may also be required. Branch protection can prevent direct pushes to `main`; create a feature branch and open a pull request instead. The bootstrap script does not create commits or push changes.