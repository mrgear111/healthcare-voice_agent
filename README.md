# Real-Time Multilingual Voice AI Agent

## Overview
A real-time voice AI agent for clinical appointment booking. The agent operates in English, Hindi, and Tamil, maintains contextual memory across sessions, and handles complex scheduling logic autonomously.

## Architecture
- **STT**: Deepgram (Nova-2) [Streaming WebSocket]
- **LLM**: OpenAI (GPT-4o-mini) [Tool Orchestration]
- **TTS**: Cartesia (Sonic) [Ultra-low latency streaming]
- **Backend**: FastAPI (Python)
- **Frontend**: Next.js (TypeScript) + Tailwind CSS
- **Memory**: Redis (Session/Short-term) + Mock Patient DB (Long-term)

## Memory Design
1. **Session Memory**: Managed via Redis. Stores current conversation turn and pending intents.
2. **Context Persistence**: Patient language preference and past interaction history are retrieved on session start and injected into the system prompt.

## Latency Breakdown
- **Target**: < 450ms
- **Optimization**: 
    - Full WebSocket pipeline (no HTTP overhead).
    - Streaming LLM tokens directly to TTS.
    - Nova-2 model for fast speech detection.

## Setup Instructions
1. **Environment Variables**: Create a `.env` file with:
   ```
   DEEPGRAM_API_KEY=your_key
   OPENAI_API_KEY=your_key
   CARTESIA_API_KEY=your_key
   REDIS_HOST=localhost
   REDIS_PORT=6379
   ```
2. **Backend**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   python -m backend.main
   ```
3. **Frontend**:
   ```bash
   cd frontend
   npm install
   npm run dev
   ```

## Known Limitations
- Barge-in handling is currently client-side.
- Outbound calling is simulated via manual trigger endpoint.
