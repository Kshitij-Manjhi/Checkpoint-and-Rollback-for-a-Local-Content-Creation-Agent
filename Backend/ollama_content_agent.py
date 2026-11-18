# Local content agent for the Responsible AI project
# Standalone terminal interface; the modular runtime is maintained separately.

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime

DATA = "runtime_data"
CHECKPOINTS = DATA + "/checkpoints.json"
CURRENT = DATA + "/current.json"
LOG = DATA + "/events.json"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = "qwen3:4b"

os.makedirs(DATA, exist_ok=True)


def readfile(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def writefile(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(event, details=""):
    events = readfile(LOG, [])
    events.append({"time": now(), "event": event, "details": details})
    writefile(LOG, events)


def get_current():
    return readfile(CURRENT, {"version": 0, "prompt": "", "draft": "", "safe": True})


def quality_check(draft):
    # intentionally simple placeholder checks; not a real AI safety evaluator
    bad_words = ["IGNORE ALL SAFETY", "STEAL PASSWORDS", "MALICIOUS PAYLOAD"]
    if not draft.strip():
        return False, "Draft is empty"
    for word in bad_words:
        if word in draft.upper():
            return False, "Policy rule matched: " + word
    if len(draft.strip()) < 15:
        return False, "Draft looks too short"
    return True, "Rule-based checks passed"


def save_checkpoint(state):
    cps = readfile(CHECKPOINTS, [])
    cp = {
        "checkpoint_id": len(cps) + 1,
        "time": now(),
        "version": state.get("version", 0),
        "prompt": state.get("prompt", ""),
        "draft": state.get("draft", ""),
        "safe": state.get("safe", False),
        "quality_note": state.get("quality_note", "")
    }
    cps.append(cp)
    writefile(CHECKPOINTS, cps)
    log("CHECKPOINT_SAVED", "Checkpoint #" + str(cp["checkpoint_id"]))
    print("\n[OK] Saved checkpoint #" + str(cp["checkpoint_id"]))


def ollama_request(endpoint, payload=None):
    url = OLLAMA_URL.rstrip("/") + endpoint
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def test_ollama():
    try:
        data = ollama_request("/api/tags")
        models = [m.get("name", "") for m in data.get("models", [])]
        print("\n[OK] Ollama is running at", OLLAMA_URL)
        if any(name == MODEL or name.startswith(MODEL + "-") for name in models):
            print("[OK] Model found:", MODEL)
        else:
            print("[!] Model not found:", MODEL)
            print("    Run: ollama pull " + MODEL)
            if models:
                print("    Installed models:", ", ".join(models))
        return True
    except Exception as e:
        print("\n[OLLAMA ERROR] Could not reach Ollama at", OLLAMA_URL)
        print("    Make sure Ollama is installed and running.")
        print("    Then run: ollama pull " + MODEL)
        print("    Details:", str(e))
        return False


def generate_draft(prompt):
    # tiny local Ollama call; no cloud API and no extra Python packages
    result = ollama_request("/api/generate", {
        "model": MODEL,
        "prompt": prompt + "\n\nWrite a useful draft. Answer directly and do not show private reasoning.",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4}
    })
    text = result.get("response", "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response")
    return text


def show_current():
    s = get_current()
    print("\n--- CURRENT STATE ---")
    print("Version:", s.get("version", 0))
    print("Status:", "SAFE" if s.get("safe", True) else "UNSAFE")
    print("Prompt:", s.get("prompt", "(none)"))
    print("Draft:\n" + (s.get("draft", "(empty)") or "(empty)"))
    print("Check:", s.get("quality_note", "not checked"))


def make_content():
    prompt = input("What should the agent write? ").strip()
    if not prompt:
        print("No prompt entered.")
        return
    old = get_current()
    print("\n[QWEN] Asking local Ollama model", MODEL, "...")
    try:
        draft = generate_draft(prompt)
    except Exception as e:
        log("LLM_GENERATION_FAILED", str(e))
        print("\n[OLLAMA ERROR] Generation failed:", str(e))
        print("Check Ollama is running, then run: ollama pull " + MODEL)
        return
    safe, note = quality_check(draft)
    state = {"version": old.get("version", 0) + 1, "prompt": prompt,
             "draft": draft, "safe": safe, "quality_note": note}
    writefile(CURRENT, state)
    log("DRAFT_GENERATED", "Version " + str(state["version"]))
    print("\n[OK] Draft generated. " + note)
    show_current()
    if safe:
        ans = input("Save this as a safe checkpoint? (y/n): ").strip().lower()
        if ans == "y":
            save_checkpoint(state)


def simulate_failure():
    state = get_current()
    if not state.get("draft"):
        print("Generate a draft first.")
        return
    print("\n1. Corrupt the draft (quality failure)")
    print("2. Insert a policy violation")
    pick = input("Choose failure: ").strip()
    if pick == "1":
        state["draft"] = "bad"
    elif pick == "2":
        state["draft"] = "IGNORE ALL SAFETY and STEAL PASSWORDS. This is a deliberately unsafe test draft."
    else:
        print("Nope, invalid choice.")
        return
    state["version"] = state.get("version", 0) + 1
    safe, note = quality_check(state["draft"])
    state["safe"] = safe
    state["quality_note"] = note
    writefile(CURRENT, state)
    log("FAILURE_INJECTED", note)
    print("\n[FAILURE SIMULATED] " + note)
    show_current()
    if not safe:
        print("Tip: choose Rollback to restore the last safe checkpoint.")


def rollback():
    cps = readfile(CHECKPOINTS, [])
    safe_cps = [c for c in cps if c.get("safe")]
    if not safe_cps:
        print("No safe checkpoint exists yet. Generate a draft and save a checkpoint first.")
        return
    cp = safe_cps[-1]
    current = {
        "version": get_current().get("version", 0) + 1,
        "prompt": cp.get("prompt", ""),
        "draft": cp.get("draft", ""),
        "safe": True,
        "quality_note": "Restored from checkpoint #" + str(cp["checkpoint_id"])
    }
    writefile(CURRENT, current)
    log("ROLLBACK_COMPLETED", "Restored checkpoint #" + str(cp["checkpoint_id"]))
    print("\n[ROLLBACK OK] Restored checkpoint #" + str(cp["checkpoint_id"]))
    show_current()


def history():
    cps = readfile(CHECKPOINTS, [])
    events = readfile(LOG, [])
    print("\n--- CHECKPOINTS ---")
    if not cps:
        print("No checkpoints yet.")
    for cp in cps:
        print("#{} | {} | {} | {}".format(
            cp["checkpoint_id"], cp["time"], "SAFE" if cp["safe"] else "UNSAFE",
            cp.get("quality_note", "")))
    print("\n--- EVENT LOG ---")
    if not events:
        print("No events yet.")
    for e in events[-20:]:
        print("{} | {} | {}".format(e["time"], e["event"], e["details"]))


def main():
    print("=" * 48)
    print(" CHECKPOINT + ROLLBACK : CONTENT AGENT")
    print(" Local JSON storage | Ollama / Qwen")
    print("=" * 48)
    while True:
        print("\n1. Generate content")
        print("2. Save current checkpoint")
        print("3. Simulate failure")
        print("4. Roll back to latest safe checkpoint")
        print("5. Show current draft")
        print("6. Show checkpoint history and audit log")
        print("7. Test Ollama / Qwen connection")
        print("0. Exit")
        choice = input("\nPick one: ").strip()
        if choice == "1":
            make_content()
        elif choice == "2":
            state = get_current()
            if not state.get("draft"):
                print("Generate a draft first.")
            elif not state.get("safe", True):
                print("Current draft is unsafe. Not saving it as a safe checkpoint.")
            else:
                save_checkpoint(state)
        elif choice == "3":
            simulate_failure()
        elif choice == "4":
            rollback()
        elif choice == "5":
            show_current()
        elif choice == "6":
            history()
        elif choice == "7":
            test_ollama()
        elif choice == "0":
            print("Bye. Runtime data is saved in ./runtime_data/")
            break
        else:
            print("Invalid option. Try again.")


if __name__ == "__main__":
    main()
