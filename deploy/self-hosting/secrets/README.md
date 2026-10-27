# Runtime Secrets

Place the three operator-managed secret files named by `.env` in this directory.
Do not commit their values. The GitHub webhook secret, GitHub App PEM private
key, and OpenAI API key are mounted only into the services that consume them.

Restrict access to the deployment operator and the container runtime. On
macOS or Linux, use `chmod 600` on each file and keep this directory at
`chmod 700`. On Windows, use an ACL that grants only the operator and the
Docker Desktop service account. The secret files must be non-empty. Trailing
whitespace is trimmed for the webhook secret and OpenAI API key; PEM content is
read as bytes.
