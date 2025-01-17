import types
import warnings

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from chatgpt.ChatService import ChatService
from chatgpt.chatLimit import handle_request_limit, clean_dict
from chatgpt.reverseProxy import chatgpt_reverse_proxy
from utils.Logger import logger
from utils.authorization import verify_token, token_list
from utils.config import api_prefix
from utils.config import enable_limit, limit_status_code
from utils.retry import async_retry

warnings.filterwarnings("ignore")

app = FastAPI()
templates = Jinja2Templates(directory="templates")
scheduler = BackgroundScheduler()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def app_start():
    scheduler.add_job(id='updateLimit_run', func=clean_dict, trigger='cron', hour=3, minute=0)
    scheduler.start()


async def to_send_conversation(request_data, access_token):
    chat_service = ChatService(access_token)
    try:
        await chat_service.set_dynamic_data(request_data)
        await chat_service.get_chat_requirements()
        return chat_service
    except HTTPException as e:
        await chat_service.close_client()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, token=Depends(verify_token)):
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})

    if enable_limit:
        limit_response = await handle_request_limit(request_data, token)
        if limit_response:
            raise HTTPException(status_code=int(limit_status_code), detail=limit_response)

    chat_service = await async_retry(to_send_conversation, request_data, token)
    try:
        await chat_service.prepare_send_conversation()
        res = await chat_service.send_conversation()
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(chat_service.close_client)
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background)
    except HTTPException as e:
        await chat_service.close_client()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(token_list)
    return templates.TemplateResponse("tokens.html", {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            token_list.append(line.strip())
            with open("data/token.txt", "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token list count: {len(token_list)}")
    tokens_count = len(token_list)
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def upload_post():
    token_list.clear()
    with open("data/token.txt", "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token list count: {len(token_list)}")
    tokens_count = len(token_list)
    return {"status": "success", "tokens_count": tokens_count}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def reverse_proxy(request: Request, path: str):
    return await chatgpt_reverse_proxy(request, path)
