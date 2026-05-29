from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.sample_data import build_dashboard, build_review

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"page_title": "财务合规审查工作台", "data": build_dashboard()},
    )


@router.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"page_title": "任务总览", "data": build_dashboard()},
    )


@router.get("/reviews/demo-001", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "review.html",
        {"page_title": "审查结果", "data": build_review()},
    )
