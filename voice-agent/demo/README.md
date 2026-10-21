# Buzz browser demo

Talk or type to Buzz in Chrome. No server build needed.

    python3 demo/server.py              # scripted answers, works offline
    LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:7b-instruct python3 demo/server.py   # Ollama
    LLM_BASE_URL=http://<inference-host>:8000/v1 LLM_MODEL=buzz-llm LLM_API_KEY=... python3 demo/server.py   # vLLM

Open http://localhost:8080. Voice uses the browser's speech recognition and speech synthesis (Chrome; microphone needs localhost or HTTPS).
If the model is unreachable the server falls back to scripted answers. Facts marked TBC in `agents/front-desk/knowledge_base.md` are answered as "I don't have that yet".
This demo is not the production pipeline (Asterisk, Dograh, Speaches); it shows the persona and conversation only.
