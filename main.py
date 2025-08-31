import os
import json
import base64
import asyncio
import websockets
import requests
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.8))
SYSTEM_MESSAGE = (
    "You are a helpful and bubbly AI assistant who loves to chat about solo respona en esapñol "
    "anything the user is interested in and is prepared to offer them facts. "
    "You have a penchant for dad jokes, owl jokes, and rickrolling – subtly. "
    "Always stay positive, but work in a joke when appropriate."
)
VOICE = 'cedar'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated'
]
SHOW_TIMING_MATH = False

# Webhook configuration
N8N_WEBHOOK_URL = "https://n8n.agentewhatsapp.co/webhook/consumoRealTimeBandidos"

# Token pricing per 1M tokens (in USD)
PRICING = {
    "text": {"input": 4.00, "cached": 0.40, "output": 16.00},
    "audio": {"input": 32.00, "cached": 0.40, "output": 64.00}
}

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

def calculate_cost(usage_data):
    """Calculate the cost based on token usage."""
    if not usage_data:
        return 0.0
    
    total_cost = 0.0
    
    # Text tokens cost calculation
    text_input = usage_data.get('input_token_details', {}).get('text_tokens', 0)
    text_cached = usage_data.get('input_token_details', {}).get('cached_tokens_details', {}).get('text_tokens', 0)
    text_output = usage_data.get('output_token_details', {}).get('text_tokens', 0)
    
    total_cost += (text_input / 1_000_000) * PRICING['text']['input']
    total_cost += (text_cached / 1_000_000) * PRICING['text']['cached']
    total_cost += (text_output / 1_000_000) * PRICING['text']['output']
    
    # Audio tokens cost calculation
    audio_input = usage_data.get('input_token_details', {}).get('audio_tokens', 0)
    audio_cached = usage_data.get('input_token_details', {}).get('cached_tokens_details', {}).get('audio_tokens', 0)
    audio_output = usage_data.get('output_token_details', {}).get('audio_tokens', 0)
    
    total_cost += (audio_input / 1_000_000) * PRICING['audio']['input']
    total_cost += (audio_cached / 1_000_000) * PRICING['audio']['cached']
    total_cost += (audio_output / 1_000_000) * PRICING['audio']['output']
    
    return round(total_cost, 6)

async def send_usage_to_webhook(usage_data, total_cost, conversation_id, caller_number=None, called_number=None, call_sid=None):
    """Send usage data and cost to N8N webhook."""
    try:
        payload = {
            "conversation_id": conversation_id,
            "usage": usage_data,
            "total_cost_usd": total_cost,
            "timestamp": asyncio.get_event_loop().time(),
            "pricing_model": PRICING,
            "call_info": {
                "from_number": caller_number,
                "to_number": called_number,
                "call_sid": call_sid
            }
        }
        
        print(f"Sending webhook to: {N8N_WEBHOOK_URL}")
        print(f"Payload: {payload}")
        
        response = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        print(f"Usage data sent to webhook successfully. Status: {response.status_code}, Cost: ${total_cost}")
        
    except Exception as e:
        print(f"Error sending usage data to webhook: {e}")
        import traceback
        traceback.print_exc()

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    # Get caller information from request
    try:
        if request.method == "POST":
            form_data = await request.form()
        else:
            form_data = request.query_params
    except Exception as e:
        print(f"Error parsing request data: {e}")
        form_data = {}
    
    caller_number = form_data.get('From', 'Unknown')
    called_number = form_data.get('To', 'Unknown')
    call_sid = form_data.get('CallSid', 'Unknown')
    
    print(f"Incoming call from {caller_number} to {called_number}, CallSid: {call_sid}")
    
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    # Pass caller info as URL parameters (URL encode them)
    from urllib.parse import quote
    encoded_from = quote(str(caller_number))
    encoded_to = quote(str(called_number))
    encoded_callsid = quote(str(call_sid))
    connect.stream(url=f'wss://{host}/media-stream?from={encoded_from}&to={encoded_to}&callsid={encoded_callsid}')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()
    
    # Extract caller info from query parameters
    query_params = websocket.query_params
    caller_number = query_params.get('from', 'Unknown')
    called_number = query_params.get('to', 'Unknown')
    call_sid = query_params.get('callsid', 'Unknown')
    
    print(f"WebSocket connection established for call from {caller_number} to {called_number}")

    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?model=gpt-realtime&temperature={TEMPERATURE}&voice={VOICE}",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        conversation_id = None
        total_usage = {
            'total_tokens': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'input_token_details': {'text_tokens': 0, 'audio_tokens': 0, 'cached_tokens': 0, 'cached_tokens_details': {'text_tokens': 0, 'audio_tokens': 0}},
            'output_token_details': {'text_tokens': 0, 'audio_tokens': 0}
        }
        
        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.state.name == 'OPEN':
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected via WebSocketDisconnect.")
                # Send final usage data to webhook before closing
                print(f"Disconnect: conversation_id={conversation_id}, tokens={total_usage.get('total_tokens', 0)}")
                if conversation_id and total_usage['total_tokens'] > 0:
                    total_cost = calculate_cost(total_usage)
                    print(f"Sending webhook on disconnect: {caller_number}, {called_number}, {call_sid}")
                    await send_usage_to_webhook(total_usage, total_cost, conversation_id, caller_number, called_number, call_sid)
                if openai_ws.state.name == 'OPEN':
                    await openai_ws.close()
            except Exception as disconnect_error:
                print(f"Client disconnected with error: {disconnect_error}")
                # Send final usage data to webhook before closing
                print(f"Error disconnect: conversation_id={conversation_id}, tokens={total_usage.get('total_tokens', 0)}")
                if conversation_id and total_usage['total_tokens'] > 0:
                    total_cost = calculate_cost(total_usage)
                    print(f"Sending webhook on error disconnect: {caller_number}, {called_number}, {call_sid}")
                    await send_usage_to_webhook(total_usage, total_cost, conversation_id, caller_number, called_number, call_sid)
                if openai_ws.state.name == 'OPEN':
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, conversation_id, total_usage
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)
                    
                    # Track conversation_id and accumulate usage data
                    if response.get('type') == 'response.done':
                        response_data = response.get('response', {})
                        if not conversation_id:
                            conversation_id = response_data.get('conversation_id')
                        
                        # Accumulate usage data
                        usage = response_data.get('usage', {})
                        if usage:
                            print(f"Accumulating usage data: {usage}")
                            total_usage['total_tokens'] += usage.get('total_tokens', 0)
                            total_usage['input_tokens'] += usage.get('input_tokens', 0)
                            total_usage['output_tokens'] += usage.get('output_tokens', 0)
                            
                            # Accumulate input token details
                            input_details = usage.get('input_token_details', {})
                            total_usage['input_token_details']['text_tokens'] += input_details.get('text_tokens', 0)
                            total_usage['input_token_details']['audio_tokens'] += input_details.get('audio_tokens', 0)
                            total_usage['input_token_details']['cached_tokens'] += input_details.get('cached_tokens', 0)
                            
                            cached_details = input_details.get('cached_tokens_details', {})
                            total_usage['input_token_details']['cached_tokens_details']['text_tokens'] += cached_details.get('text_tokens', 0)
                            total_usage['input_token_details']['cached_tokens_details']['audio_tokens'] += cached_details.get('audio_tokens', 0)
                            
                            # Accumulate output token details
                            output_details = usage.get('output_token_details', {})
                            total_usage['output_token_details']['text_tokens'] += output_details.get('text_tokens', 0)
                            total_usage['output_token_details']['audio_tokens'] += output_details.get('audio_tokens', 0)

                    if response.get('type') == 'response.output_audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)


                        if response.get("item_id") and response["item_id"] != last_assistant_item:
                            response_start_timestamp_twilio = latest_media_timestamp
                            last_assistant_item = response["item_id"]
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")
                # Send final usage data to webhook on error
                if conversation_id and total_usage['total_tokens'] > 0:
                    total_cost = calculate_cost(total_usage)
                    await send_usage_to_webhook(total_usage, total_cost, conversation_id, caller_number, called_number, call_sid)

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        try:
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
        finally:
            # Ensure usage data is sent even if connection ends normally
            print(f"Connection ended. Conversation ID: {conversation_id}, Total tokens: {total_usage.get('total_tokens', 0)}")
            if conversation_id and total_usage['total_tokens'] > 0:
                total_cost = calculate_cost(total_usage)
                print(f"Sending final usage data: {total_usage}")
                await send_usage_to_webhook(total_usage, total_cost, conversation_id, caller_number, called_number, call_sid)
            else:
                print(f"Not sending webhook - conversation_id: {conversation_id}, tokens: {total_usage.get('total_tokens', 0)}")

async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "hola"
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"}
                },
                "output": {
                    "format": {"type": "audio/pcmu"}
                }
            },
            "instructions": SYSTEM_MESSAGE,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # AI speaks first with initial greeting
    await send_initial_conversation_item(openai_ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
