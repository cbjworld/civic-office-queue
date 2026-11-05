"""One intranet address for existing office callers. No central ticket database."""
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import requests
import uvicorn

from queue_api import Identity, JoinRequest, LeaveRequest
from organization import ALL_DEPTS, ORGANIZATION, config


def address(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise ValueError("개별 PC의 IPv4 주소를 입력하세요.")
    return str(ip)


def settings(path):
    values = json.loads(path.read_text(encoding="utf-8-sig"))["portal"]
    bind = address(values["listen"])
    hosts = values["servers"]
    if not isinstance(hosts, dict) or set(hosts) - set(ALL_DEPTS):
        raise ValueError("servers의 간부실 이름이 offices와 일치해야 합니다.")
    return bind, {name: address(ip) for name, ip in hosts.items() if ip}


def create_portal(hosts):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    page = Path(__file__).with_name("portal.html").read_text(encoding="utf-8-sig")

    @app.get("/")
    def index():
        return HTMLResponse(page, headers={"Cache-Control":"no-store","X-Frame-Options":"DENY"})

    @app.get("/offices")
    def offices():
        return {"organization": ORGANIZATION, "offices": ALL_DEPTS}

    def relay(dept, action, body):
        if dept not in hosts:
            raise HTTPException(503, "호출기 주소가 설정되지 않았습니다.")
        path = "state" if action == "state" else action + "/" + quote(dept, safe="")
        with requests.Session() as session:
            session.trust_env = False
            try:
                resp = session.post(f"http://{hosts[dept]}:8000/v2/{path}", json=body.model_dump(mode="json"), timeout=(1.5, 2), allow_redirects=False)
                if resp.status_code == 409:
                    raise HTTPException(409, resp.json().get("detail", "대기 상태를 다시 확인하세요."))
                if resp.status_code != 200:
                    raise ValueError()
                state = resp.json()["offices"][dept]
                return JSONResponse(state, headers={"Cache-Control":"no-store"})
            except (requests.RequestException, ValueError, TypeError, KeyError):
                raise HTTPException(503, "호출기 연결 또는 간부실 설정을 확인하세요.") from None

    @app.post("/office/{dept}/state")
    def state(dept: str, body: Identity):
        return relay(dept, "state", body)

    @app.post("/office/{dept}/join")
    def join(dept: str, body: JoinRequest):
        return relay(dept, "join", body)

    @app.post("/office/{dept}/leave")
    def leave(dept: str, body: LeaveRequest):
        return relay(dept, "leave", body)
    return app


if __name__ == "__main__":
    try:
        host, rooms = settings(config)
        print(f"직원 통합 대기표: http://{host}:8080/  (종료: Ctrl+C)", flush=True)
        uvicorn.run(create_portal(rooms), host=host, port=8080, access_log=False)
    except (ValueError, KeyError, OSError, SystemExit) as exc:
        print("실행 실패: organization.json의 portal 설정과 8080 포트를 확인하세요.", exc)
        if getattr(sys, "frozen", False):
            input("Enter를 누르면 닫힙니다.")
        raise
