"""Buzz demo server: serves index.html and answers POST /chat.

Standard library only. The reply comes from the first backend that is set up:
  1. an OpenAI-compatible endpoint (LLM_BASE_URL, LLM_MODEL, optional LLM_API_KEY),
     which is vLLM `buzz-llm` on the inference server, or Ollama locally;
  2. otherwise a scripted fallback built from the front-desk knowledge block,
     so the demo always runs, even with no model and no internet.
The system prompt is agents/front-desk/global_prompt.md plus knowledge_base.md.

  python3 demo/server.py            then open http://localhost:8080 in Chrome
"""
import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "agents" / "front-desk"
BASE = os.environ.get("LLM_BASE_URL", "").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "buzz-llm")
KEY = os.environ.get("LLM_API_KEY", "")
MAX_TURNS = 12

SYSTEM = (AGENT / "global_prompt.md").read_text() + "\n\n" + (AGENT / "knowledge_base.md").read_text()
sessions = {}

FAQ = [
    (r"\b(stop calling|opt out|remove me|do not call)\b", "Understood. I have noted that you don't want further calls, and I'll make sure that is respected."),
    (r"\b(human|person|staff|reception|transfer|speak to)\b", "Of course. In a live call I would transfer you to reception now. For this demo, I can take a message instead."),
    (r"\b(women|woman)\b", "Women in Tech Zambia is for women-led, tech-based businesses. Ten participants get training, mentorship and equity-free funding, from June to September 2026. I can take your details so the team explains how to apply."),
    (r"\b(accelerator|ai for business)\b", "The AI for Business Accelerator 2026 was a twelve-week intensive for African startups building AI solutions, with mentorship, corporate pilots and up to fifty thousand dollars in follow-on funding. Applications closed on the twenty-fourth of July."),
    (r"\b(green|climate|ndc)\b", "The FACE NDC Green Innovation Challenge supports ten Zambian green-tech startups in agriculture, forestry and renewable energy, over six months in Lusaka."),
    (r"\b(programmes?|programs?|apply|intake|incubat)\b", "We run Women in Tech Zambia, the FACE NDC Green Innovation Challenge, the AI for Business Accelerator, and a human-centred design course, among others. Which one would you like to hear about? I can also take your details so the team can follow up on how to apply."),
    (r"\b(ai lab|artificial|assessment|data|machine)\b", "The BongoHive AI Lab helps businesses, governments and communities adopt AI. It offers a free AI assessment, training sessions, AI tools and end-to-end support, plus free research such as State of AI in Zambia 2024."),
    (r"\b(price|cost|how much|fee)\b", "We don't publish prices. The team will share details, and I can take a message or help you book a tour."),
    (r"\b(plans?|membership|desk|office|cowork|workspace|day pass|tour)\b", "Our coworking plans are Hot Desk, Hot Desk Plus, Hot Desk Premium with twenty-four hour access, a Private Office, and a Virtual Office. There is fast fibre internet, meeting rooms and free parking. You can book a free day pass or a tour on our website, or I can take a message."),
    (r"\b(hours?|open|opening|close|closing|saturday|sunday|weekend)\b", "We are open Monday to Friday, eight to five, and Saturday, nine to one. We are closed on Sundays."),
    (r"\b(parking|park)\b", "Yes, there is free on-site parking in the compound, with no time limit."),
    (r"\b(where|address|located|location|directions?)\b", "We are at Hive Coworking, ZEP-RE Business Park, Mass Media, Alick Nkhata Road, Lusaka. There is free parking on site."),
    (r"\b(email|e-mail)\b", "You can email contactus at bongohive dot co dot zm."),
    (r"\b(phone|number|call you|contact)\b", "You can reach us on plus two six zero, nine five three, zero one seven, five two six, or email contactus at bongohive dot co dot zm."),
    (r"\b(events?|workshop|meetup|pitch|innovation edge)\b", "We hold monthly workshops, networking events and pitch nights, and the Innovation Edge peer event every two months. I don't have the next dates yet, but I can take your email so we send them to you."),
    (r"\b(universit|student|college|school)\b", "Yes, we host student tours on the last Friday of every month, and partner with universities on training, incubation and hackathons. Please contact the team through your institution."),
    (r"\b(about|what is bongohive|what do you do|tell me)\b", "BongoHive is Zambia's leading innovation hub, founded in 2011. We offer coworking, technology and innovation consulting, the AI Lab, and entrepreneurship support. We have backed over two hundred startups."),
    (r"\b(hello|hi|hey|good (morning|afternoon|evening))\b", "Hello! I'm Buzz from BongoHive AI Lab. How can I help?"),
    (r"\b(who are you|what are you|are you (an )?ai|robot)\b", "I'm Buzz, an AI assistant for BongoHive AI Lab. I'm not a person."),
    (r"\b(thanks?|thank you|bye|goodbye)\b", "You're welcome. Goodbye!"),
]


def scripted(message):
    low = message.lower()
    for pattern, answer in FAQ:
        if re.search(pattern, low):
            return answer
    return "I'm not sure about that. I can take your name and number so someone from the team calls you back."


def llm(history):
    body = json.dumps({"model": MODEL, "messages": [{"role": "system", "content": SYSTEM}] + history,
                       "max_tokens": 160, "temperature": 0.3}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", body,
                                 {"Content-Type": "application/json", **({"Authorization": "Bearer " + KEY} if KEY else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def reply_for(sid, message):
    history = sessions.setdefault(sid, [])
    history.append({"role": "user", "content": message})
    answer = None
    if BASE:
        try:
            answer = llm(history[-MAX_TURNS:])
        except Exception as e:  # fall back so the demo never dies on stage
            print("LLM failed, using scripted fallback:", e)
    answer = answer or scripted(message)
    history.append({"role": "assistant", "content": answer})
    return answer


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", (HERE / "index.html").read_bytes())
        elif self.path.startswith("/branding/svg/") and self.path.endswith(".svg") and ".." not in self.path:
            f = HERE.parent / self.path.lstrip("/")
            if f.is_file():
                self._send(200, "image/svg+xml", f.read_bytes())
            else:
                self._send(404, "text/plain", b"not found")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path != "/chat":
            return self._send(404, "text/plain", b"not found")
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            reply = reply_for(str(data["sessionId"])[:64], str(data["message"])[:500])
            self._send(200, "application/json", json.dumps({"reply": reply}).encode())
        except Exception:
            self._send(400, "application/json", b'{"error":"bad request"}')


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"Buzz demo on http://localhost:{port}  backend: {'LLM ' + BASE if BASE else 'scripted fallback'}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
