# Deploying to AWS EC2

This runs the webhook service in Docker on a single EC2 instance, behind Caddy for automatic HTTPS.
Cost: a `t3.small` is enough (the heavy lifting happens in the Claude API).

## 1. Launch the instance

1. EC2 → Launch instance → Ubuntu 24.04, `t3.small`, 20 GB disk.
2. Security group: allow inbound 22 (your IP only), 80 and 443 (anywhere).
3. Allocate an Elastic IP and attach it, so the webhook URL doesn't change.
4. Point a domain at it, or use `<elastic-ip>.nip.io` for a free hostname.

## 2. Install Docker and Caddy

```bash
ssh ubuntu@<elastic-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && newgrp docker
sudo apt-get install -y caddy
```

## 3. Run the service

```bash
git clone https://github.com/vkxr/mcp-code-review-agent.git && cd mcp-code-review-agent
cp .env.example .env && nano .env           # fill in keys and secrets
docker build -t review-agent .
docker volume create review-data
docker run -d --name review-agent --restart unless-stopped \
  --env-file .env -v review-data:/data -p 127.0.0.1:8000:8000 review-agent
curl localhost:8000/health
```

The port is bound to 127.0.0.1 so only Caddy can reach it.

## 4. HTTPS with Caddy

`/etc/caddy/Caddyfile`:

```
review.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl reload caddy
```

## 5. Connect GitHub

Repo → Settings → Webhooks → Add webhook:

- Payload URL: `https://review.example.com/webhook`
- Content type: `application/json`
- Secret: the same value as `GITHUB_WEBHOOK_SECRET`
- Events: "Pull requests" only

## 6. Approve reviews

```bash
export H="X-Admin-Token: <ADMIN_TOKEN>"
curl -H "$H" https://review.example.com/reviews?status=awaiting_approval
curl -H "$H" https://review.example.com/reviews/<id>
curl -H "$H" -H "Content-Type: application/json" -d '{"approved": true}' \
  https://review.example.com/reviews/<id>/decision
```

## Updating

```bash
git pull && docker build -t review-agent . && docker rm -f review-agent && <the docker run command above>
```

Pending reviews survive restarts because graph state is checkpointed to SQLite on the `review-data` volume.

## Security notes

- The test runner is off in the server (`enable_tests=False`). Running tests means executing code from
  the pull request; only enable it for trusted repos, in a disposable container.
- Use a fine-grained GitHub token limited to the repos you review.
- Stop the instance when you're not demoing it to avoid charges.
