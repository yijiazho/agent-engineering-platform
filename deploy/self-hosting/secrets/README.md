# Runtime Secrets

Place the three operator-managed secret files named by `.env` in this directory.
Do not commit their values. The GitHub webhook secret, GitHub App PEM private
key, and OpenAI API key are mounted only into the services that consume them.
