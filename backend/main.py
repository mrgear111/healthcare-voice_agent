import asyncio
import json
import os
from dotenv import load_dotenv
load_dotenv()
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pipeline.stt import DeepgramHandler
from pipeline.llm import LLMService
from pipeline.tts import DeepgramTTSHandler
from services.appointment import AppointmentService

from services.tools import get_appointment_tools
from memory.manager import MemoryManager

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()
appointment_service = AppointmentService()
llm_service = LLMService()
tts_handler = DeepgramTTSHandler()
memory_manager = MemoryManager()
active_sessions = {} # Track {session_id: task} for interruption

async def interrupt_session(session_id, websocket):
    interrupted = False
    if session_id in active_sessions:
        task = active_sessions[session_id]
        if not task.done():
            logger.info(f"🚫 Interrupting session {session_id}")
            task.cancel()
            interrupted = True
            try:
                await task
            except asyncio.CancelledError:
                pass
        active_sessions.pop(session_id, None)
    
    # Only clear audio if we actually stopped an ongoing process
    if interrupted and websocket:
        await websocket.send_json({"type": "clear_audio"})

async def stream_to_tts(text_generator, websocket, language="en", on_audio_start=None):
    """ Helper to pipe text stream to tts and then to websocket """
    await tts_handler.stream_audio(text_generator, websocket, language=language, on_audio_start=on_audio_start)

async def llm_callback(text, is_final=False, websocket=None, session_id="default", language="en", confidence=0.0, start_time=None):
    logger.debug(f"llm_callback trace: text='{text}', is_final={is_final}, conf={confidence}")
    if not is_final and text is not None:
        return

    logger.info(f"Executing LLM for: {text} (Session: {session_id}, Language: {language})")
    try:
        # Start timing for metrics
        import time
        t0 = start_time or time.time()
        
        async def on_audio_start():
            latency = int((time.time() - t0) * 1000)
            logger.info(f"📊 Latency Metric: {latency}ms")
            if websocket:
                await websocket.send_json({
                    "type": "metrics",
                    "latency": latency,
                    "confidence": confidence
                })

        # 1. Fetch persistent history (Requirement: Awareness of patient context)
        conversation_history = memory_manager.get_session(session_id)
        if not conversation_history:
            system_prompt = llm_service.get_system_prompt(language=language)
            conversation_history = [{"role": "system", "content": system_prompt}]
        
        conversation_history.append({"role": "user", "content": text})
        tools = get_appointment_tools()
        
        # 2. Start LLM Stream
        response_stream = await llm_service.get_response(conversation_history, tools=tools)
        
        # 3. Setup Async Queue for TTS piping
        text_queue = asyncio.Queue()
        async def text_iterator():
            while True:
                val = await text_queue.get()
                if val is None: break
                yield val

        # Start TTS streaming in background (Requirement: Standard Indian language support)
        tts_task = asyncio.create_task(stream_to_tts(text_iterator(), websocket, language=language, on_audio_start=on_audio_start))

        full_response = ""
        buffer = ""
        tool_use = None
        tool_input_json = ""
        
        async for event in response_stream:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    content = event.delta.text
                    full_response += content
                    buffer += content
                    
                    # Split by common sentence endings to reduce TTS jitter
                    if any(p in content for p in [".", "?", "!", "\n"]):
                        await text_queue.put(buffer)
                        buffer = ""
                        
                    if websocket:
                        await websocket.send_json({"type": "llm_text", "content": content})
                elif event.delta.type == "input_json_delta":
                    tool_input_json += event.delta.partial_json
            elif event.type == "content_block_start":
                if event.content_block.type == "tool_use":
                    tool_use = event.content_block
                    # Reasoning trace (Requirement: visible reasoning trace)
                    if websocket:
                        await websocket.send_json({"type": "reasoning", "content": f"Thinking: Calling tool {tool_use.name}..."})

        # Signal end of text to TTS
        if buffer:
            await text_queue.put(buffer)
        await text_queue.put(None)
        await tts_task

        # 4. Process Tool Call if any
        if tool_use:
            tool_name = tool_use.name
            tool_id = tool_use.id
            import json
            args = json.loads(tool_input_json)
            
            logger.info(f"Running tool {tool_name} with args {args}")
            result = await llm_service.run_tool(tool_name, args, appointment_service)
            
            # Send tool result to UI
            if websocket:
                await websocket.send_json({"type": "reasoning", "content": f"Tool Result: {result}"})

            # Update history with tool use and result
            conversation_history.append({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": full_response},
                    {"type": "tool_use", "id": tool_id, "name": tool_name, "input": args}
                ]
            })
            conversation_history.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(result)
                    }
                ]
            })
            
            # Recursive call for final response
            await llm_callback(None, True, websocket, session_id, language, confidence=confidence, start_time=t0)
        
        elif full_response:
            conversation_history.append({"role": "assistant", "content": full_response})
            memory_manager.save_session(session_id, conversation_history)
            logger.info(f"Agent: {full_response}")
        
        memory_manager.save_session(session_id, conversation_history)
    except asyncio.CancelledError:
        logger.info(f"Task for {session_id} was cancelled (interrupted).")
        raise
    except Exception as e:
        logger.error(f"CRITICAL ERROR in llm_callback: {e}", exc_info=True)
        if websocket:
            await websocket.send_json({"type": "error", "message": str(e)})

@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket, session_id: str = "default", language: str = "en", sample_rate: int = 48000):
    await websocket.accept()
    logger.info(f"Client connected: session={session_id}, lang={language}, rate={sample_rate}")
    
    # 5. Turn Management & Speech Detection (Requirement: Turn Management & VAD)
    async def wrapped_callback(text, is_final=False, confidence=0.0):
        # Record start time for latency measurement
        import time
        start_time = time.time() if is_final else None

        # Immediate UI feedback for transcription
        await websocket.send_json({
            "type": "stt_text", 
            "content": text, 
            "is_final": is_final
        })
        # Proceed to LLM only when final
        if is_final:
            logger.info(f"STT Final -> Triggering LLM: {text} (conf={confidence:.2f})")
            # Run LLM in background so we don't block the STT/Audio loop
            task = asyncio.create_task(llm_callback(text, is_final, websocket, session_id, language, confidence, start_time))
            active_sessions[session_id] = task

    stt_handler = DeepgramHandler(callback=wrapped_callback)
    await stt_handler.start(language=language + "-IN" if language != "en" else "en-US", sample_rate=sample_rate)
    
    try:
        while True:
            data = await websocket.receive_bytes()
            await stt_handler.send_data(data)
    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error in {session_id}: {e}")
    finally:
        await stt_handler.stop()

@app.post("/campaign/trigger")
async def trigger_campaign(patient_id: str, topic: str):
    # In practice, this would initiate a call
    # For the demo, we log and return success
    logger.info(f"Manual campaign trigger: {patient_id} for {topic}")
    return {"status": "triggered", "patient_id": patient_id}

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
