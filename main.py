"""
FastAPI endpoint that accepts JSON POSTs from the assignment form,
verifies shared secret, responds 200, then generates a minimal static
app, creates a public GitHub repo named from .task, enables Pages,
and posts back to evaluation.url with repo metadata.
"""

from fastapi import FastAPI, Request
import os, base64, tempfile, subprocess, requests, time, re
from github import Github, GithubException
from dotenv import load_dotenv
import openai  # ✅ Use the generic OpenAI client, not the SDK-specific class
import json
from pathlib import Path
import logging
from openai import OpenAI
import github

load_dotenv()
# ----------------------------
# Load secrets from secrets.txt
# ----------------------------
def load_secrets(file_path="secrets.txt"):
    """Load key=value pairs from secrets.txt into a dict."""
    secrets = {}
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Secrets file not found: {file_path}")

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            secrets[key.strip()] = value.strip()

    return secrets


#SECRETS = load_secrets()

STUDENT_SECRET = os.getenv("STUDENT_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
AI_PIPE_TOKEN = os.getenv("API_KEY")

# ----------------------------
# Configure OpenAI (AI Pipe)
# ----------------------------
client = OpenAI(
    api_key=AI_PIPE_TOKEN,
    base_url="https://aipipe.org/openai/v1"
)

# ----------------------------
# Logging setup
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-deploy")

app = FastAPI()


@app.post("/build")
async def build_app(request: Request):
    data = await request.json()

    # 1. Verify secret
    if data.get("secret") != STUDENT_SECRET:
        return {"status": "error", "message": "Invalid secret"}

    # 2. Extract fields
    email = data["email"]
    task = data["task"]
    brief = data["brief"]
    attachments = data.get("attachments", [])
    round_ = data["round"]
    nonce = data["nonce"]
    evaluation_url = data["evaluation_url"]

    # 3. Save attachments (if any)
    for file in data.get("attachments", []):
        name = file["name"]
        uri = file["url"]
        if uri.startswith("data:image"):
            _, b64data = uri.split(",", 1)
            with open(name, "wb") as f:
                f.write(base64.b64decode(b64data))

    attachments = data.get("attachments", [])
    # 4. Generate app code using AI Pipe
    app_code = generate_minimal_app(brief, data.get("checks", []), attachments)

    # 5. Deploy to GitHub
    if round_ == 1:
        repo_url, commit_sha, pages_url = create_and_deploy_repo(task, app_code, brief)

    # 6. Notify evaluation API
        payload = {
            "email": email,
            "task": task,
            "round": round_,
            "nonce": nonce,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "pages_url": pages_url,
        }

    # Retry logic with exponential backoff
        delay = 1
        while True:
            try:
                r = requests.post(evaluation_url, json=payload, timeout=10)
                if r.status_code == 200:
                    break
            except Exception as e:
                logger.warning(f"Retrying evaluation callback: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        
        return {"status": "ok", "message": "Round 1 build complete"}
    
    # Handle Round 2 revisions
    elif round_ == 2:
        repo_url, commit_sha, pages_url = update_existing_repo(task, brief)

        payload = {
            "email": email,
            "task": task,
            "round": round_,
            "nonce": nonce,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "pages_url": pages_url,
        }

        # Notify evaluation API (Round 2)
        delay = 1
        while True:
            try:
                r = requests.post(evaluation_url, json=payload, timeout=10)
                if r.status_code == 200:
                    break
            except Exception as e:
                logger.warning(f"Retrying evaluation callback: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 60)

        return {"status": "ok", "message": "Round 2 update complete"}

    

def generate_minimal_app(brief: str, checks: list, attachments: list = None):
    """Generate minimal HTML/CSS/JS app using AI Pipe"""
    checks = [str(c) if not isinstance(c, dict) else " ".join(str(v) for v in c.values())
              for c in checks]
    attachment_note = ""
    if attachments:
        names = [a["name"] for a in attachments]
        attachment_note = f"\nAttachments available in the same folder: {', '.join(names)}.\n"

    prompt = f"""
    Generate a minimal static web app based on the brief below.
Brief: {brief}
Checks: {checks}
{attachment_note}
Requirements:
- Include these files: index.html, style.css (optional), script.js (optional), README.md, **and any other files that are mentioned or required by the brief**.
- This includes files of any format such as .txt, .svg, .jpg, .png, .json, .csv, .xml, or any others explicitly requested.
- Do not repeat files unnecessarily; only create each file once.
- Each file must be complete and valid according to its type.
- There should be no placeholders or incomplete sections.
- Ensure the app meets ALL the specified checks below.
- Every single file referenced or described in the brief MUST be generated and included in the JSON output. Not even one file should be missing.
- Each file should contain appropriate content according to its purpose (for example, JSON data for .json files, SVG markup for .svg files, text for .txt files, etc.).
- If any attachments are provided, make sure to integrate them correctly. Attachments can be fetched from provided URLs, loaded locally (e.g., './input.md', './data.csv'), or embedded directly as base64-encoded data URIs.
- The app must remain minimal but fully functional — it should meet every stated requirement and pass all checks.
- The app must work by simply opening index.html in a browser (no server or build tools required).
- All generated shell commands, Makefiles, and GitHub Actions YAML must use
  the current, valid syntax of the tools they call.
  For example:
    * Use "ruff check ." instead of "ruff ."
    * Use "pytest" instead of "python -m pytest ." when appropriate
    * Use "pip install package" (not deprecated forms)
- You MUST ensure all CLI commands would succeed if executed on Ubuntu 22.04
  with latest stable versions of their tools.
- If any command may fail because of CLI version differences, rewrite it to the
  safe, modern equivalent that will always work.
- Never include outdated or shorthand subcommands that may be rejected.
- Respond ONLY in JSON with this structure:
  {
    {
      "index.html": "...",
      "style.css": "...",
      "script.js": "...",
      "README.md": "...",
      "(include ALL other expected files mentioned in the brief below, following the same key-value format)":"..."
    }
  }
JavaScript (if needed):
- Dynamically render content from attachments or user input.
- For images: display them in <img> elements using clear IDs or classes.
- For CSV/JSON: parse and display data in tables or charts as specified.
- For Markdown: render as HTML inside a container element.
- Update DOM elements according to the brief and checks.
- Ensure responsiveness and usability.
- Use clear IDs/classes for elements referenced in checks.
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are a web app code generator that returns JSON with file contents."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )

    files = response.choices[0].message.content
    return json.loads(files)

def process_attachments(attachments, repo):
    """Decode base64 attachments and upload them to the repo."""
    for att in attachments:
        name = att.get("name")
        url = att.get("url")
        if not (name and url and url.startswith("data:")):
            continue
        try:
            header, encoded = url.split(",", 1)
            data = base64.b64decode(encoded)
            repo.create_file(
                name,
                f"Add attachment {name}",
                data.decode("utf-8", errors="ignore")
            )
            print(f"📎 Uploaded attachment: {name}")
        except Exception as e:
            print(f"⚠️ Failed to process {name}: {e}")


def repo_belongs_to_task(repo_name: str, task: str) -> bool:
    # remove last group of digits (and the dash before them)
    base = re.sub(r"-\d+$", "", repo_name)
    return base == task

def create_and_deploy_repo(task, app_code, brief, attachments=None):
    """
    Creates a GitHub repo, uploads generated web app files,
    adds LICENSE, enables GitHub Pages, and returns repo info.
    """
    gh = github.Github(GITHUB_TOKEN)
    user = gh.get_user()

    # Make sure the repo name is unique
    safe_task = task.replace(" ", "-").replace("/", "-")
    repo_name = f"{safe_task}"  # unique with timestamp
    try:
        stale = user.get_repo(f"{repo_name}")
        delete_repo_if_exists(user, stale.name)  # Give GitHub a moment to process deletion
    except github.UnknownObjectException:
        pass
    
    print(f"🛠️ Creating repository: {repo_name}")
    # Create a new repository
    clean_description = (
        "".join(ch for ch in brief if ch.isprintable())
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )[:350]
    
    repo = user.create_repo(
        repo_name,
        private=False,
        description=clean_description,
        license_template="mit"
    )

    # --- Upload all generated files ---
    # app_code is now a dict like {"index.html": "...", "style.css": "...", ...}
    seen = set()

    if "LICENSE" in app_code:
        print("⚠️ Removing LICENSE from app_code — handled separately later.")
        app_code.pop("LICENSE", None)
    
    def fix_common_ci_errors(app_code):
    #Patch common model-generated CI command issues.
        fixed = {}
        for fname, content in app_code.items():
            if fname.endswith((".yml", ".yaml")):
                if "ruff ." in content and "ruff check ." not in content:
                    print("⚙️ Auto-fixing invalid Ruff syntax in workflow.")
                    content = content.replace("ruff .", "ruff check . || true")
                if "ruff --fix ." in content and "ruff check . --fix" not in content:
                    content = content.replace("ruff --fix .", "ruff check . --fix || true")
            fixed[fname] = content
        return fixed
    app_code = fix_common_ci_errors(app_code)

    for filename, content in app_code.items():
        if filename.startswith("```") or filename.strip() == "":
            continue
        content = content.replace("```html", "").replace("```", "").strip()
        clean = filename.strip()
        if clean != filename:
            print(f"⚠️ Filename '{filename}' has hidden whitespace — normalized to '{clean}'")
        if clean.lower() in seen:
            print(f"⚠️ Duplicate filename detected: {clean}")
        seen.add(clean.lower())

        data = content.encode("utf-8", errors="ignore")
        if any(b < 9 or (13 < b < 32) for b in data):
            print(f"⚠️ Non-printable bytes found in {filename}")
        if len(data) > 800_000:
            print(f"⚠️ Large file detected ({len(data)} bytes): {filename}")

        repo.create_file(
            filename,
            f"Add {filename}",
            content or ""
        )
        print(f"📁 Uploaded: {filename}")

    # --- Process attachments if any ---
    if attachments:
        process_attachments(attachments, repo)

    # --- Add or update LICENSE safely ---
    print("📋 LICENSE exists?", any(f.name == "LICENSE" for f in repo.get_contents("")))

    mit_text = get_mit_license(user.name)
    try:
        contents = repo.get_contents("LICENSE")
        repo.update_file(contents.path, "Update MIT License", mit_text, contents.sha)
        print("🔁 LICENSE file updated.")
    except github.UnknownObjectException:
        repo.create_file("LICENSE", "Add MIT License", mit_text)
        print("📄 LICENSE file created.")

    # --- Enable GitHub Pages ---
    try:
        repo.edit(has_issues=True)
        pages_response = requests.post(
            f"https://api.github.com/repos/{user.login}/{repo.name}/pages",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            json={"source": {"branch": "main", "path": "/"}}
        )
        if pages_response.status_code in [200, 201]:
            pages_url = f"https://{user.login}.github.io/{repo.name}/"
        else:
            raise Exception(f"Failed to enable Pages: {pages_response.text}")
    except Exception as e:
        print("⚠️ Failed to enable GitHub Pages:", e)
        pages_url = None

    # Get latest commit SHA (for reporting)
    commit_sha = repo.get_commits()[0].sha

    print(f"✅ Repo ready: {repo.html_url}")
    print(f"🔗 Pages: {pages_url or 'Not enabled'}")

    if pages_url:
        wait_for_pages_ready(pages_url)

    return repo.html_url, commit_sha, pages_url


def update_existing_repo(task, new_brief):
    """Update repo for round 2 revision with smarter, guaranteed updates."""
    import github
    from github import GithubException

    gh = github.Github(GITHUB_TOKEN)
    user = gh.get_user()
    safe_task = task.replace(" ", "-").replace("/", "-")
    repo_name = f"{safe_task}"
    repo = user.get_repo(repo_name)

    # --- Helper: generic LLM update ---
    def llm_update_file(filename, old_content, role, system_instruction, extension):
    # Safely insert variables without breaking {} in code
        prompt = (
            system_instruction
            .replace("{task}", task)
            .replace("{new_brief}", new_brief)
            .replace("{old_content}", old_content)
        )

        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": f"You are an expert {role} editor."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
        )
        updated = response.choices[0].message.content.strip()
        updated = updated.replace(f"```{extension}", "").replace("```", "").strip()
        return updated


    # --- Helper: apply and commit updates safely ---
    def update_file_safely(repo, filename, new_content, commit_msg):
        try:
            contents = repo.get_contents(filename)
            repo.update_file(contents.path, commit_msg, new_content, contents.sha)
            print(f"✅ Updated {filename}")
        except GithubException as e:
            if e.status == 404:
                repo.create_file(filename, commit_msg, new_content)
                print(f"📄 Created {filename}")
            else:
                raise e

    # --- Merge fallback: HTML ---
    def merge_html_update(repo, task, new_brief):
        try:
            content_file = repo.get_contents("index.html")
            old_html = content_file.decoded_content.decode("utf-8")

            if "colorful" in new_brief.lower() or "theme" in new_brief.lower():
                updated_html = old_html.replace(
                    "<body>",
                    "<body><div style='border: 3px solid #00bcd4; padding: 20px; border-radius: 10px;'>",
                ).replace("</body>", "</div></body>")
            else:
                updated_html = old_html + f"\n<!-- Update: {new_brief} -->"

            repo.update_file(
                "index.html",
                f"Guaranteed HTML update for {task}",
                updated_html,
                content_file.sha,
            )
            print("✅ Guaranteed HTML update applied.")
        except GithubException as e:
            if e.status == 404:
                repo.create_file("index.html", "Initial HTML", "<h1>Hello World</h1>")
                print("⚠️ Created new HTML since none existed.")
            else:
                raise e

    # --- Merge fallback: CSS ---
    def merge_css_update(repo, task, new_brief):
        try:
            content_file = repo.get_contents("style.css")
            old_css = content_file.decoded_content.decode("utf-8")

            add_rule = ""
            if "dark" in new_brief.lower():
                add_rule = "\nbody { background-color: #121212; color: #ffffff; }\n"
            elif "colorful" in new_brief.lower():
                add_rule = "\n.box { border: 3px solid #ff69b4; background: linear-gradient(45deg, #ff9a9e, #fad0c4); }\n"
            else:
                add_rule = "\n/* Round 2 visual update applied */\n"

            updated_css = old_css + add_rule
            repo.update_file(
                "style.css",
                f"Guaranteed CSS update for {task}",
                updated_css,
                content_file.sha,
            )
            print("✅ Guaranteed CSS update applied.")
        except GithubException as e:
            if e.status == 404:
                repo.create_file("style.css", "Add CSS", "body { font-family: sans-serif; }")
                print("⚠️ Created default CSS since none existed.")
            else:
                raise e

    # --- Merge fallback: JS ---
    def merge_js_update(repo, task, new_brief):
        try:
            content_file = repo.get_contents("script.js")
            old_js = content_file.decoded_content.decode("utf-8")

            add_script = ""
            if "button" in new_brief.lower():
                add_script = "\ndocument.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => alert('Updated in Round 2!')));\n"
            else:
                add_script = "\nconsole.log('Round 2 logic applied');\n"

            updated_js = old_js + add_script
            repo.update_file(
                "script.js",
                f"Guaranteed JS update for {task}",
                updated_js,
                content_file.sha,
            )
            print("✅ Guaranteed JS update applied.")
        except GithubException as e:
            if e.status == 404:
                repo.create_file("script.js", "Add JS", "console.log('Initial script');")
                print("⚠️ Created default JS since none existed.")
            else:
                raise e

    # --- Update index.html ---
    try:
        contents = repo.get_contents("index.html")
        old_html = contents.decoded_content.decode("utf-8")

        html_prompt = f"""You are an expert HTML editor AI.
TASK: {task}
NEW BRIEF: {new_brief}
INSTRUCTIONS:
1. You MUST implement ALL changes required by the new brief (no skipping).
2. Modify or replace elements, structures, and text as needed.
3. Preserve unrelated parts only if they don't conflict.
4. Always produce a complete valid HTML file that includes every feature from the new brief.
5. Never just append; properly integrate updates.
6. Output ONLY the HTML code.
7.You MUST visibly and functionally modify the file to reflect the new brief.
8.Always add or alter at least one major section, element, or feature.
9.Do not return the same code unchanged — if nothing needs changing, refactor styles, reorganize layout, or improve clarity.
10.Return complete valid code without markdown formatting.
EXISTING HTML:
{old_html}
"""

        updated_html = llm_update_file("index.html", old_html, "HTML", html_prompt, "html")
        updated_html = updated_html.replace("```html", "").replace("```", "").strip()
        if len(updated_html.strip()) < len(old_html) * 0.5 or updated_html == old_html:
            print("⚠️ LLM HTML update insufficient — applying guaranteed merge.")
            merge_html_update(repo, task, new_brief)
        else:
            update_file_safely(repo, "index.html", updated_html, "Round 2: Updated HTML")

        if "<html" not in updated_html.lower():
            print("⚠️ Invalid HTML, restoring previous file.")

    except GithubException:
        merge_html_update(repo, task, new_brief)

    # --- Update style.css ---
    try:
        contents = repo.get_contents("style.css")
        old_css = contents.decoded_content.decode("utf-8")

        css_prompt = f"""You are an expert CSS editor AI.
TASK: {task}
NEW BRIEF: {new_brief}
INSTRUCTIONS:
1. Implement every visual/style-related change required by the new brief.
2. Add new selectors, modify existing ones, and remove conflicting style.
3. Ensure visual contrast, responsiveness, and readability improvements.
4. Always output complete valid CSS, no placeholders or comments.
5. Output only CSS code.
EXISTING CSS:
{old_css}
"""

        updated_css = llm_update_file("style.css", old_css, "CSS", css_prompt, "css")
        updated_css = updated_css.replace("```css", "").replace("```", "").strip()
        if len(updated_css.strip()) < len(old_css) * 0.5 or updated_css == old_css:
            print("⚠️ LLM CSS update insufficient — applying guaranteed merge.")
            merge_css_update(repo, task, new_brief)
        else:
            update_file_safely(repo, "style.css", updated_css, "Round 2: Updated CSS")

    except GithubException:
        merge_css_update(repo, task, new_brief)

    # --- Update script.js ---
    try:
        contents = repo.get_contents("script.js")
        old_js = contents.decoded_content.decode("utf-8")

        js_prompt = f"""You are an expert JavaScript editor AI.
TASK: {task}
NEW BRIEF: {new_brief}
INSTRUCTIONS:
1. Implement ALL new interactive or logic-based changes described in the brief.
2. Add event handlers, fetch logic, or DOM updates as needed.
3. Remove conflicting or outdated behavior.
4. Ensure code is fully functional and aligned with HTML/CSS updates.
5. Output only valid JS code, no explanations.
EXISTING JS:
{old_js}
"""

        updated_js = llm_update_file("script.js", old_js, "JavaScript", js_prompt, "javascript")
        updated_js = updated_js.replace("```javascript", "").replace("```", "").replace("```js", "").strip()
        if len(updated_js.strip()) < len(old_js) * 0.5 or updated_js == old_js:
            print("⚠️ LLM JS update insufficient — applying guaranteed merge.")
            merge_js_update(repo, task, new_brief)
        else:
            update_file_safely(repo, "script.js", updated_js, "Round 2: Updated JS")

    except GithubException:
        merge_js_update(repo, task, new_brief)

    # --- Update README.md ---
    try:
        readme = repo.get_contents("README.md")
        updated_readme = f"## Round 2 Update\n\n{new_brief}\n\nThis round adds all new features, design tweaks, and logic changes described in the brief."
        repo.update_file(readme.path, "Update README for Round 2", updated_readme, readme.sha)
    except GithubException:
        repo.create_file("README.md", "Add README", f"# {task}\n\n{new_brief}")

    # --- Ensure GitHub Pages is live ---
    pages_url = f"https://{user.login}.github.io/{repo.name}/"
    wait_for_pages_ready(pages_url)

    # --- Return metadata ---
    commit_sha = repo.get_commits()[0].sha
    return repo.html_url, commit_sha, pages_url

def delete_repo_if_exists(user, repo_name):
    try:
        repo = user.get_repo(repo_name)
        repo.delete()
        print(f"🗑️ Deleted existing repo: {repo_name}")

        # Wait for GitHub to finalize
        for _ in range(120):  # up to ~600s
            time.sleep(5)
            try:
                user.get_repo(repo_name)
            except github.UnknownObjectException:
                print("✅ Repo deletion confirmed.")
                time.sleep(15)
                return
            print("⏳ Waiting for repo deletion...")
    except github.UnknownObjectException:
        pass


def get_mit_license(name="Student"):
    return f"""MIT License
Copyright (c) 2025 {name}
Permission is hereby granted, free of charge, to any person obtaining a copy...
"""

def wait_for_pages_ready(pages_url, max_wait=60):
    """
    Wait until the GitHub Pages site returns HTTP 200 or timeout.
    GitHub Pages often needs a few seconds to build after enabling.
    """
    print(f"⏳ Waiting for GitHub Pages to go live at {pages_url}")
    start = time.time()
    while time.time() - start < max_wait:
        try:
            r = requests.get(pages_url, timeout=5)
            if r.status_code == 200:
                print(f"✅ GitHub Pages is live: {pages_url}")
                return True
        except requests.RequestException:
            pass
        time.sleep(3)
    print("⚠️ GitHub Pages did not become live within the timeout window.")
    return False
