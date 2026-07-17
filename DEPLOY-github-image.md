# Deploy via a prebuilt image (no server-side build)

Use this if Portainer can't build images (the "frame too large" / BuildKit error)
or you don't want to deal with Git auth on the server. GitHub builds the image;
Portainer just pulls it.

## 1. Push the repo (with the workflow) to GitHub

Make sure `.github/workflows/docker-image.yml` is in the repo, then push to `main`.
Go to the repo's **Actions** tab and watch "Build and push Chopper image" run.
When it's green, your image exists at:

    ghcr.io/<your-username>/chopper-service:latest

## 2. Make the image public (one time)

So Portainer can pull without credentials:
GitHub → your profile → **Packages** → `chopper-service` → **Package settings**
→ **Change visibility → Public**.

(If you'd rather keep it private, instead add GHCR registry credentials in
Portainer: Registries → Add registry → Custom, `ghcr.io`, your username, and a
PAT with `read:packages`.)

## 3. Deploy in Portainer (web editor — no Git, no build)

- Edit `docker-compose.yml`: replace `OWNER` with your GitHub username (lowercase).
- Portainer → **Stacks → Add stack → Web editor** → paste the compose.
- Under **Environment variables** add:
  - `CHOPPER_API_SECRET` = a long random string
  - `CHOPPER_MODEL` = `llama3.2:3b` (optional; this is the default)
- **Deploy the stack.** It pulls the images and starts — no build step.

## 4. Pull the model (one time)

Portainer → Containers → `chopper-ollama` → Console/Exec → `/bin/sh`:

    ollama pull llama3.2:3b

## 5. Test

`http://<docker-server-ip>:8000/health` → `{"ok": true, ...}`

## Updating Chopper's notes later

Because the notes are baked into the image, to change them you edit the `.md`
files in `data/notes/` (or `data/personas.json`), push to `main`, let the Action
rebuild, then in Portainer **re-pull / redeploy** the stack (`pull_policy: always`
means a redeploy grabs the new image).
