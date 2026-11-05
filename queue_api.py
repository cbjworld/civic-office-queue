"""Employee API. Administrator actions stay inside the office application."""
import socket
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from queue_core import QueueError

HTTP_PORT = 8000


class Identity(BaseModel):
    client_id: uuid.UUID


class JoinRequest(Identity):
    name: str = Field(min_length=1, max_length=50)
    department: str = Field(default='', max_length=80)


class LeaveRequest(Identity):
    ticket_id: uuid.UUID


def create_api(store, focus=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(QueueError)
    async def queue_error(request, exc):
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.get('/focus_window')
    async def focus_window(request: Request):
        if not request.client or request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(403)
        if focus:
            focus()
        return {'status': 'focused'}

    @app.post('/v2/state')
    async def state(data: Identity):
        return store.state(str(data.client_id))

    @app.post('/v2/join/{dept}')
    async def join(dept: str, data: JoinRequest):
        return store.join(dept, str(data.client_id), data.name, data.department)

    @app.post('/v2/leave/{dept}')
    async def leave(dept: str, data: LeaveRequest):
        return store.leave(dept, str(data.client_id), str(data.ticket_id))
    return app


def run_discovery(departments, stop, sock):
    sock.settimeout(.5)
    response = f"SERVER_INFO_V2:{HTTP_PORT}:{','.join(departments)}".encode('utf-8')
    while not stop.is_set():
        try:
            message, address = sock.recvfrom(1024)
            if message == b'FIND_SERVER_REQUEST_V2':
                sock.sendto(response, address)
        except socket.timeout:
            continue
        except OSError:
            break
