import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_inspection import check  # noqa: E402

GOOD = """role: inference
### lsb_release -a
Release:\t24.04
### nvidia-smi
| NVIDIA-SMI 580.95   Driver Version: 580.95.05  CUDA Version: 13.0 |
|  0MiB / 23034MiB |
### df -h
/dev/vda1  500G   40G  460G   9% /
### ss -tulpn
tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
### docker version
Server: Docker Engine
 Version:          27.3.1
"""


def by_name(text):
    return {n: (s, d) for n, s, d in check(text)[1]}


def test_good_inference_host_passes():
    r = by_name(GOOD)
    assert r["GPU memory"][0] == "ok" and "24 GB" in r["GPU memory"][1]
    assert r["NVIDIA driver"][0] == "ok"
    assert r["Ports free"][0] == "ok"


def test_port_clash_fails():
    r = by_name(GOOD.replace("0.0.0.0:22 ", "0.0.0.0:8000 "))
    assert r["Ports free"][0] == "fail"


def test_missing_gpu_fails():
    r = by_name(GOOD.replace("|  0MiB / 23034MiB |", "NOT-INSTALLED: nvidia-smi"))
    assert r["GPU memory"][0] == "fail"


def test_small_gpu_fails():
    assert by_name(GOOD.replace("23034MiB", "8192MiB"))["GPU memory"][0] == "fail"
