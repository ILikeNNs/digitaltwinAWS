import os
import json
import gradio as gr
from openai import OpenAI
# Import modular layers
from database import init_sql_db, log_conversation, log_contact_request
from rag_engine import RAGEngine
from dotenv import load_dotenv
# Persona and model settings live in helpers so the serverless deployment shares them
from helpers import (
    tools, SYSTEM_BASE_PROMPT, MODEL, TEMPERATURE, MAX_TOOL_ROUNDS, MAX_COMPLETION_TOKENS,
    MAX_MESSAGE_LENGTH, MAX_SESSION_TURNS, LOG_RETENTION_DAYS, GENERIC_ERROR_REPLY, SESSION_LIMIT_REPLY,
    sanitize_input, model_history, stream_reply,
)

load_dotenv(override=True)

api_key = os.getenv('OPENAI_API_KEY')
buildchroma = True

PDF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linkedin.pdf")

# Initialize OpenAI Client (Make sure OPENAI_API_KEY is in your environment)
client = OpenAI()

# ----------------------------------------------------
# 2. Database Initialization
# ----------------------------------------------------
rag = RAGEngine(api_key=api_key)
init_sql_db()
if buildchroma:
    rag.index_pdf(PDF_PATH)

# ----------------------------------------------------
# 3. Chat Logic
# ----------------------------------------------------

def handle_tool_calls(message):
    messages = []
    for tool_call in message.tool_calls:
        arguments = json.loads(tool_call.function.arguments)
        if tool_call.function.name == "query_resume":
            tool_output = rag.retrieve_context(query=arguments.get("query"))
            print('used agent for rag')
        elif tool_call.function.name == "save_contact_info":
            tool_output = log_contact_request(
                name=arguments.get("name"),
                email=arguments.get("email"),
                message=arguments.get("message")
                )
            print('used agent for saving contacts')
        else:
            tool_output = "Error: Tool not found."
        messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": tool_call.function.name,
                "content": tool_output,
            })
    return messages



def chat_stream(history):
    """Streams the assistant reply into the conversation as it arrives, then logs the exchange.

    Yields the full history after every received text fragment, so Gradio can re-render the reply live.
    """
    user_question = history[-1]["content"] if history else ""
    history = [{"role":h["role"], "content":h["content"]} for h in history]
    reply = ""

    if sum(1 for h in history if h["role"] == "user") > MAX_SESSION_TURNS:
        reply = SESSION_LIMIT_REPLY
    else:
        # Only recent user/assistant turns reach the model, so a long session cannot grow the prompt without bound
        messages = [{"role": "system", "content": SYSTEM_BASE_PROMPT}] + model_history(history)
        try:
            for fragment in stream_reply(client, messages, handle_tool_calls):
                reply += fragment
                yield history + [{"role":"assistant", "content":reply}]
        except Exception as e:
            # Details go to the server log only: the app is public and error text can leak internals.
            # Keep whatever was already streamed so the visitor's screen doesn't jump.
            print(f"chat failed: {e!r}")
            reply = f"{reply}\n\n{GENERIC_ERROR_REPLY}" if reply else GENERIC_ERROR_REPLY

    # Show the reply (or the error) in the chat either way, so the UI never appears to hang
    history += [{"role":"assistant", "content":reply}]
    try:
        log_conversation(user_question, reply)
    except Exception as e:
        # A full disk or locked database must not take the chat down with it
        print(f"logging failed: {e!r}")
    yield history


def chat(history):
    """Non-streaming variant of chat_stream: returns the final history."""
    for history in chat_stream(history):
        pass
    return history

# ----------------------------------------------------
# 4. Gradio Interface Construction
# ----------------------------------------------------
# Callbacks (along with the chat() function above)

def put_message_in_chatbot(message, history):
        return "", history + [{"role":"user", "content":sanitize_input(message, max_length=MAX_MESSAGE_LENGTH)}]

def submit_message(message, history):
    message, history = put_message_in_chatbot(message, history)
    return message, history, history

def answer(history):
    # A generator handler makes Gradio stream every partial reply to the browser
    for updated in chat_stream(history):
        yield updated, updated

# UI definition

with gr.Blocks() as ui:
    # The conversation lives in server-side session state; the Chatbot only displays it.
    # Reading history from the Chatbot would let a client rewrite earlier turns or inject system messages.
    history_state = gr.State([])
    with gr.Row():
        chatbot = gr.Chatbot(height=500)
    with gr.Row():
        message = gr.Textbox(label="Chat with my digital twin:", max_length=MAX_MESSAGE_LENGTH)
    gr.Markdown(
        f"Conversations are logged to improve this assistant and deleted after {LOG_RETENTION_DAYS} days. "
        "Contact details you share are stored only so I can get back to you."
    )

    # Hooking up events to callbacks; "private" keeps them out of the public Gradio API
    message.submit(
        submit_message, inputs=[message, history_state], outputs=[message, history_state, chatbot],
        api_visibility="private"
    ).then(
        answer, inputs=history_state, outputs=[history_state, chatbot], api_visibility="private"
    )

if __name__ == "__main__":
    # Run locally
    ui.launch(inbrowser=True)