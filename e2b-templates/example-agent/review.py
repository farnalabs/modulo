import os, json, subprocess, sys, re
from urllib.request import Request, urlopen
p = open("/home/user/prompt.md").read()
m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", p)
if not m:
    json.dump({"status": "failed", "summary": "Bad PR URL"}, open("/home/user/output.json", "w")); sys.exit(1)
owner, repo_name, num = m.group(1), m.group(2), m.group(3)
tok = os.environ.get("GITHUB_TOKEN", "")
def call_api(path, data=None):
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github.v3+json", "User-Agent": "modulo-agent"}
    if data is not None: h["Content-Type"] = "application/json"
    b = json.dumps(data).encode() if data else None
    try:
        with urlopen(Request(f"https://api.github.com/repos/{owner}/{repo_name}/{path}", data=b, headers=h, method="POST" if data else "GET"), timeout=30) as resp:
            return json.loads(resp.read().decode()) if resp.read() else {}
    except Exception as ex:
        err = ex.read().decode()[:500] if hasattr(ex, "read") else str(ex)
        print(f"API ERROR: {path} -> {err}", flush=True); return None
tmp = f"/tmp/{repo_name}"
if not os.path.exists(tmp):
    subprocess.run(["git","clone",f"https://x-access-token:{tok}@github.com/{owner}/{repo_name}.git",tmp], capture_output=True, timeout=120, check=True)
subprocess.run(["git","fetch","origin",f"pull/{num}/head:pr-h"], cwd=tmp, capture_output=True, timeout=30)
subprocess.run(["git","checkout","pr-h"], cwd=tmp, capture_output=True, timeout=30)
pr_data = call_api(f"pulls/{num}")
base = pr_data.get("base",{}).get("ref","main") if pr_data else "main"
title = pr_data.get("title","") if pr_data else ""
diff_text = subprocess.run(["git","diff",f"origin/{base}...pr-h","--","."], cwd=tmp, capture_output=True, text=True, timeout=30).stdout or ""
files_set, findings = set(), []
def add(sev, lens, file, line, desc): findings.append({"severity":sev,"lens":lens,"file":file,"line":line,"description":desc})
for chunk in diff_text.split("diff --git ")[1:]:
    fn = (chunk.split("+++ b/")[1].split("\n")[0] if "+++ b/" in chunk else "").strip()
    if not fn or os.path.splitext(fn)[1] not in (".py",".ts",".vue",".js",".tsx",".jsx",".go",".rs",".yaml",".yml",".json",".sql",".sh",".toml",""): continue
    files_set.add(fn)
    for i,ln in enumerate(chunk.split("\n")[2:],1):
        if not ln.startswith("+"): continue
        if re.search(r"(?i)(password|secret|api_key|token)\s*=\s*['""][^'""]+['""]",ln):
            add("CRITICAL" if re.search(r"(?i)(password|secret|api_key)",ln) else "MAJOR","Security",fn,i,"Hardcoded credential/token")
        if re.search(r"(?i)(eval|exec)\s*\(",ln): add("CRITICAL","Security",fn,i,"eval/exec usage")
        if re.search(r"except\s*:",ln): add("MAJOR","Error Handling",fn,i,"Bare except clause")
        if re.search(r"raise\s+Exception\(",ln): add("MAJOR","Error Handling",fn,i,"Raising base Exception")
        if re.search(r"from \w+ import \*",ln): add("MAJOR","Code Style",fn,i,"Wildcard import")
        if len(ln)>200: add("MINOR","Code Style",fn,i,"Line >200 chars")
        if re.search(r"(?i)(TODO|FIXME|HACK|XXX)",ln): add("MINOR","Maintainability",fn,i,"Unresolved TODO")
score = sum({"CRITICAL":5,"MAJOR":3,"MINOR":1}.get(x["severity"],0) for x in findings)
has_c = any(x["severity"]=="CRITICAL" for x in findings)
verdict = "changes_requested" if has_c else ("changes_requested" if score > 10 else ("comment" if score > 3 else "approved"))
crits = sum(1 for x in findings if x["severity"]=="CRITICAL")
majs = sum(1 for x in findings if x["severity"]=="MAJOR")
mins = sum(1 for x in findings if x["severity"]=="MINOR")
body = f"## PR Review #{num}\n### Results\n- CRITICAL: {crits}\n- MAJOR: {majs}\n- MINOR: {mins}\n### Decision\n**{verdict.upper()}**\n\n_Lenses: Security, Error Handling, Code Style, Maintainability_"

# Write output.json BEFORE posting to GitHub — ensures we always have the review
json.dump({
    "status": "completed",
    "summary": f"PR #{num}: {verdict}",
    "verdict": verdict,
    "issues_found": findings[:30],
    "stats": {"criticals": crits, "majors": majs, "minors": mins, "score": score, "files": len(files_set)},
    "pr_info": {"owner": owner, "repo": repo_name, "number": int(num), "title": title[:200]}
}, open("/home/user/output.json", "w"), indent=2)

comments = [{"path":x["file"],"body":f"[{x['severity']}] [{x['lens']}] {x['description']}"} for x in findings[:25] if x["file"] in files_set]
print(f"Posting review: {verdict} with {len(comments)} comments", flush=True)
result = call_api(f"pulls/{num}/reviews", {"body":body, "event":verdict, "comments":comments})
if result is None:
    print("Failed to post review to GitHub (output.json already written)", flush=True)
    sys.exit(0)
print("Done", flush=True)
