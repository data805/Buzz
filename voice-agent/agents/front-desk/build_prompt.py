"""Build the Global node prompt: global_prompt.md (below the first '---' line)
plus knowledge_base.md (HTML comments and the title removed), and write it into
workflow.json. Run after editing either file, then lint.

  python3 agents/front-desk/build_prompt.py          # write
  python3 agents/front-desk/build_prompt.py --check  # exit 1 if workflow.json is stale
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_WORDS = 2000  # about 3k tokens (PLAN D2)


def build():
    prompt = (HERE / "global_prompt.md").read_text().split("\n---\n", 1)[1].strip()
    kb = re.sub(r"<!--.*?-->", "", (HERE / "knowledge_base.md").read_text(), flags=re.S)
    kb = re.sub(r"^# .*\n", "", kb).strip()
    return prompt + "\n\n# Knowledge block\n\n" + kb


def main(check):
    text = build()
    words = len(text.split())
    if words > MAX_WORDS:
        sys.exit(f"prompt is {words} words, over the {MAX_WORDS} budget")
    path = HERE / "workflow.json"
    wf = json.loads(path.read_text())
    glob = [n for n in wf["workflow_definition"]["nodes"] if n["type"] == "globalNode"]
    if len(glob) != 1:
        sys.exit("expected exactly one globalNode")
    if glob[0]["data"]["prompt"] == text:
        print(f"workflow.json is up to date ({words} words)")
        return 0
    if check:
        print("workflow.json is stale: run build_prompt.py")
        return 1
    glob[0]["data"]["prompt"] = text
    path.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"updated workflow.json ({words} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
