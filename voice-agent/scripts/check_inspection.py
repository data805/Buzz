"""Read an inspection file from scripts/inspect-server.sh and say whether the
host meets the PLAN section 11 / section 4 needs. Prints a verdict per check."""
import re
import sys
from pathlib import Path

PORTS = {
    "inference": {8000: "vLLM", 8001: "Speaches", 9400: "DCGM exporter", 9100: "node-exporter"},
    "app": {80: "nginx", 443: "nginx", 3478: "coturn", 5060: "Asterisk SIP", 8088: "Asterisk ARI"},
}


def sections(text):
    parts = re.split(r"^### (.+)$", text, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def check(text):
    role = (re.search(r"^role: (\w+)", text, re.M) or [None, "?"])[1]
    s = sections(text)
    results = []

    def add(name, status, detail):
        results.append((name, status, detail))

    rel = s.get("lsb_release -a", "")
    m = re.search(r"Release:\s*(\S+)", rel)
    if m:
        add("Ubuntu", "ok" if m.group(1) in ("22.04", "24.04") else "warn", m.group(1))
    else:
        add("Ubuntu", "fail", "lsb_release missing or unreadable")

    smi = s.get("nvidia-smi", "")
    if role == "inference":
        mem = re.search(r"(\d+)MiB\s*/\s*(\d+)MiB", smi)
        drv = re.search(r"Driver Version:\s*([\d.]+)", smi)
        if mem:
            gb = int(mem.group(2)) / 1024
            preset = 48 if gb >= 40 else 24 if gb >= 20 else 16 if gb >= 15 else None
            add("GPU memory", "ok" if preset else "fail",
                f"{gb:.0f} GB, preset {preset} GB" if preset else f"{gb:.0f} GB is below the 16 GB minimum")
        else:
            add("GPU memory", "fail", "no GPU reported by nvidia-smi")
        if drv:
            major = int(drv.group(1).split(".")[0])
            add("NVIDIA driver", "ok" if major >= 580 else "warn",
                f"{drv.group(1)} (plan pins the R580 branch)")
        else:
            add("NVIDIA driver", "fail", "not found")

    disk = s.get("df -h", "")
    root = re.search(r"\s(\d+(?:\.\d+)?)([GT])\s+\d+(?:\.\d+)?[GTMK]?\s+\S+\s+\d+%\s+/\s*$", disk, re.M)
    if root:
        free_gb = float(root.group(1)) * (1024 if root.group(2) == "T" else 1)
        need = 100 if role == "inference" else 50
        add("Disk on /", "ok" if free_gb >= need else "warn", f"{free_gb:.0f} GB size, want at least {need} GB")
    else:
        add("Disk on /", "warn", "could not read the / line")

    ss = s.get("ss -tulpn", "")
    clash = []
    for port, name in PORTS.get(role, {}).items():
        if re.search(rf"[:\]]{port}\s", ss):
            clash.append(f"{port} ({name})")
    add("Ports free", "fail" if clash else "ok", "in use: " + ", ".join(clash) if clash else "none of ours are taken")

    dock = s.get("docker version", "")
    if "NOT-INSTALLED" in dock:
        add("Docker", "warn", "not installed yet (Phase 1 installs it)")
    else:
        v = re.search(r"Server:.*?Version:\s*(\S+)", dock, re.S)
        add("Docker", "ok" if v else "warn", v.group(1) if v else "daemon not reachable")
    return role, results


def main(argv):
    if len(argv) != 2:
        print("usage: check_inspection.py <inspection file>", file=sys.stderr)
        return 2
    role, results = check(Path(argv[1]).read_text())
    print(f"Inspection verdict for role '{role}'")
    for name, status, detail in results:
        print(f"  [{status.upper():4}] {name}: {detail}")
    return 1 if any(r[1] == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
