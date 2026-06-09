#!/usr/bin/env python3
"""Local web UI that runs the user's Codex skill as a document engine."""

from __future__ import annotations

import cgi
import csv
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
JOBS_DIR = APP_DIR / "jobs"
DOWNLOADS_DIR = Path.home() / "Downloads" / "CodexMinutes"
WORKSPACE_DIR = Path(os.environ.get("CODEX_MINUTES_WORKSPACE", str(APP_DIR)))
TRANSCRIPT_SKILL_DIR = Path(
    os.environ.get(
        "TRANSCRIPT_TO_MINUTES_SKILL_DIR",
        str(Path.home() / ".codex" / "skills" / "transcript-to-minutes"),
    )
)
FACT_CHECK_SKILL_DIR = Path(
    os.environ.get(
        "FACT_CHECK_MINUTES_SKILL_DIR",
        str(Path.home() / ".codex" / "skills" / "fact-check-minutes"),
    )
)
ACADEMY_WEEKLY_SKILL_DIR = Path(
    os.environ.get(
        "ACADEMY_WEEKLY_REPORT_SKILL_DIR",
        str(APP_DIR.parent / ".agents" / "skills" / "academy-weekly-report"),
    )
)
ACADEMY_WEEKLY_SCRIPT = ACADEMY_WEEKLY_SKILL_DIR / "scripts" / "analyze_weekly_report.py"
WORKSPACE_PYTHON = Path(
    os.environ.get(
        "CODEX_WORKSPACE_PYTHON",
        str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "bin" / "python3"),
    )
)
SKILL_PROFILES = {
    "official": {
        "label": "공식 배포용",
        "skill_name": "transcript-to-minutes",
        "skill_dir": TRANSCRIPT_SKILL_DIR,
        "purpose": "공식 한국어 회의록",
        "unclear": "[확인 필요], [시간 확인 필요], [장소 확인 필요]",
        "focus": [
            "공식 배포 가능한 간결한 회의록으로 정리",
            "불필요한 잡담, 반복, 사담, 민감한 부차 발언은 제외",
            "합의사항, 결정사항, 담당자, 향후 일정을 명확히 정리",
        ],
    },
    "fact_check": {
        "label": "사실 확인용",
        "skill_name": "fact-check-minutes",
        "skill_dir": FACT_CHECK_SKILL_DIR,
        "purpose": "사실확인 회의록",
        "unclear": "[확인 필요], [시간 확인 필요], [장소 확인 필요], [발언자 확인 필요], [근거 확인 필요], [원문 확인 필요]",
        "focus": [
            "발언자별 주장, 반박, 인정, 부인, 유보, 요청을 명확히 귀속",
            "상반 진술과 근거 공백을 임의로 해소하지 않고 보존",
            "법적 판단이나 책임 확정 표현은 피하고 확인 필요 사항으로 정리",
        ],
    },
}
CODEX_BIN = (
    os.environ.get("CODEX_BIN")
    or shutil.which("codex")
    or (
        "/Applications/Codex.app/Contents/Resources/codex"
        if os.name != "nt"
        else str(Path.home() / "AppData" / "Local" / "Programs" / "Codex" / "codex.exe")
    )
)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_OPINION_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_OPINION_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_BASENAME = "260101 (프로젝트명) 000 회의록"
OPINION_DIR = APP_DIR / "opinion-data"
OPINION_UPLOAD_DIR = OPINION_DIR / "uploads"
OPINION_DB = OPINION_DIR / "opinions.json"
OPINION_ADMIN_ID = os.environ.get("OPINION_ADMIN_ID", "admin")
OPINION_ADMIN_PW = os.environ.get("OPINION_ADMIN_PW", "admin")
OPINION_ADMIN_TOKEN = os.environ.get("OPINION_ADMIN_TOKEN", "opinion-admin-local-token")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
QUEUE_LOCK = threading.Lock()


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _safe_name(value: str, default: str = DEFAULT_BASENAME) -> str:
    value = value.strip() or default
    keep = []
    for char in value:
        if char.isalnum() or char in ("-", "_", ".", " ", "(", ")"):
            keep.append(char)
        elif char.isspace():
            keep.append(" ")
    return "".join(keep).strip("._") or default


def _safe_upload_name(value: str, default: str = "upload") -> str:
    name = Path(value or default).name
    keep = []
    for char in name:
        if char.isalnum() or char in ("-", "_", "."):
            keep.append(char)
    cleaned = "".join(keep).strip("._")
    return cleaned or default


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_opinions() -> list[dict]:
    if not OPINION_DB.exists():
        return []
    try:
        data = json.loads(OPINION_DB.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _save_opinions(items: list[dict]) -> None:
    OPINION_DIR.mkdir(parents=True, exist_ok=True)
    OPINION_DB.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_opinion_id(items: list[dict]) -> str:
    numbers = []
    for item in items:
        match = re.search(r"(\d+)$", str(item.get("id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"MVP-{(max(numbers) + 1) if numbers else 1:04d}"


def _is_opinion_admin(handler: BaseHTTPRequestHandler) -> bool:
    return handler.headers.get("X-Admin-Token") == OPINION_ADMIN_TOKEN


def _opinion_matches(item: dict, filters: dict[str, str]) -> bool:
    gender = filters.get("gender", "")
    age = filters.get("age", "")
    dong = filters.get("dong", "")
    keyword = filters.get("keyword", "").lower()
    attachment = filters.get("attachment", "")
    if gender and gender != "전체" and item.get("gender") != gender:
        return False
    if age and age != "전체" and item.get("age") != age:
        return False
    if dong and dong != "전체" and item.get("dong") != dong:
        return False
    if attachment == "이미지 있음" and not item.get("attachments"):
        return False
    if attachment == "이미지 없음" and item.get("attachments"):
        return False
    if keyword:
        haystack = " ".join(str(item.get(key, "")) for key in ("name", "title", "content", "dong")).lower()
        if keyword not in haystack:
            return False
    return True


def _opinion_stats(items: list[dict]) -> dict:
    today = time.strftime("%Y-%m-%d")
    return {
        "total": len(items),
        "today": sum(1 for item in items if str(item.get("created_at", "")).startswith(today)),
        "withImage": sum(1 for item in items if item.get("attachments")),
        "dongCount": len({item.get("dong") for item in items if item.get("dong")}),
    }


def _opinion_csv(items: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["접수번호", "접수시간", "이름", "성별", "나이대", "행정동", "제목", "내용", "이미지1", "이미지2"])
    for item in items:
        attachments = item.get("attachments") or []
        writer.writerow(
            [
                item.get("id", ""),
                item.get("created_at", ""),
                item.get("name", ""),
                item.get("gender", ""),
                item.get("age", ""),
                item.get("dong", ""),
                item.get("title", ""),
                item.get("content", ""),
                (attachments[0] or {}).get("filename", "") if len(attachments) > 0 else "",
                (attachments[1] or {}).get("filename", "") if len(attachments) > 1 else "",
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _set_job(job_id: str, **updates) -> None:
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(updates)


def _job_files(job_dir: Path) -> list[dict[str, str | int]]:
    files = []
    for path in sorted(job_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in {".hwpx", ".docx", ".pdf", ".json", ".txt", ".md"}:
            files.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "url": f"/api/jobs/{job_dir.name}/download?file={path.name}",
                }
            )
    return files


def _copy_outputs_to_downloads(job_id: str, job_dir: Path) -> list[str]:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in sorted(job_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".hwpx", ".docx", ".pdf"}:
            continue
        target = _unique_download_path(DOWNLOADS_DIR / path.name)
        shutil.copy2(path, target)
        copied.append(str(target))
    return copied


def _unique_download_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")


def _force_requested_output_names(job_dir: Path, basename: str) -> list[str]:
    renamed = []
    for suffix in (".hwpx", ".docx", ".pdf", ".md"):
        candidates = [
            path
            for path in job_dir.iterdir()
            if path.is_file() and path.suffix.lower() == suffix
        ]
        if not candidates:
            continue
        source = max(candidates, key=lambda path: path.stat().st_mtime)
        target = job_dir / f"{basename}{suffix}"
        if source.resolve() == target.resolve():
            continue
        if target.exists():
            target.unlink()
        source.rename(target)
        renamed.append(f"{source.name} -> {target.name}")

    report_candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_file() and path.name.endswith(".conversion-report.json")
    ]
    if report_candidates:
        source = max(report_candidates, key=lambda path: path.stat().st_mtime)
        target = job_dir / f"{basename}.conversion-report.json"
        if source.resolve() != target.resolve():
            if target.exists():
                target.unlink()
            source.rename(target)
            renamed.append(f"{source.name} -> {target.name}")
    return renamed


def _strip_final_period(value: str) -> str:
    stripped = value.rstrip()
    trailing = value[len(stripped):]
    if stripped.endswith(".") and not stripped.endswith("..."):
        return stripped[:-1] + trailing
    return value


def _strip_final_periods(value):
    if isinstance(value, str):
        return _strip_final_period(value)
    if isinstance(value, list):
        return [_strip_final_periods(item) for item in value]
    if isinstance(value, dict):
        return {
            key: item if key == "meeting_datetime" else _strip_final_periods(item)
            for key, item in value.items()
        }
    return value


def _sanitize_minutes_json(job_dir: Path) -> bool:
    minutes_path = job_dir / "minutes.json"
    if not minutes_path.exists():
        return False
    data = json.loads(minutes_path.read_text(encoding="utf-8"))
    sanitized = _strip_final_periods(data)
    minutes_path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _regenerate_outputs_from_minutes(job_dir: Path, basename: str, formats: str, skill_dir: Path) -> dict:
    minutes_path = job_dir / "minutes.json"
    package_script = skill_dir / "scripts" / "create_minutes_package.py"
    if not minutes_path.exists() or not package_script.exists():
        return {"ok": False, "skipped": True}
    cmd = [
        sys.executable,
        str(package_script),
        str(minutes_path),
        str(job_dir),
        "--basename",
        basename,
        "--formats",
        formats,
    ]
    proc = subprocess.run(
        cmd,
        cwd=job_dir,
        text=True,
        capture_output=True,
        timeout=60 * 5,
    )
    (job_dir / "postprocess.stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (job_dir / "postprocess.stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "command": cmd}


def _codex_cmd(*args: str) -> list[str]:
    return [str(CODEX_BIN), *args]


def _codex_exists() -> bool:
    codex = str(CODEX_BIN)
    return bool(shutil.which(codex) or Path(codex).exists())


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _read_tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def _job_error_detail(job_dir: Path) -> str:
    parts = []
    for name in ("codex.stderr.log", "codex.last-message.txt", "codex.stdout.log"):
        tail = _read_tail(job_dir / name)
        if tail.strip():
            parts.append(f"--- {name} ---\n{tail.strip()}")
    return "\n\n".join(parts)


def _skill_profile(skill_mode: str) -> dict:
    return SKILL_PROFILES.get(skill_mode) or SKILL_PROFILES["official"]


def _build_prompt(
    job_dir: Path,
    transcript_path: Path,
    basename: str,
    formats: str,
    metadata: dict,
    skill_mode: str,
) -> str:
    profile = _skill_profile(skill_mode)
    metadata_lines = "\n".join(
        f"- {key}: {value}" for key, value in metadata.items() if str(value).strip()
    )
    metadata_block = metadata_lines or "- 별도 보충 정보 없음"
    focus_lines = "\n".join(f"- {item}" for item in profile["focus"])
    return f"""
${profile["skill_name"]} 스킬을 사용해서 아래 녹취록 파일을 {profile["purpose"]}으로 변환해줘.

선택한 회의록 용도:
- {profile["label"]}

작업 조건:
- 입력 녹취록 파일: {transcript_path}
- 출력 폴더: {job_dir}
- 출력 basename: {basename}
- 요청 출력 형식: {formats}
- 기본 결과물은 HWPX이며, 요청 형식에 docx/pdf가 있으면 companion format으로 같이 생성
- 반드시 구조화 JSON을 {job_dir / "minutes.json"} 에 저장
- 산출물 파일명은 반드시 `{basename}`를 그대로 사용하고, 임의로 녹취록 파일명/회의명/연도를 재구성하지 않음
- 선택한 스킬 폴더의 기존 스크립트를 사용해서 최종 파일을 생성
- 파일은 반드시 출력 폴더 안에만 생성
- 불명확한 회의일시/장소/참석자/담당자는 {profile["unclear"]} 계열 표시 유지
- 최종 답변에는 생성된 파일 경로만 간단히 정리

문장부호 규칙:
- 회의록 본문, 결정/확인 사항, 향후 일정/후속 확인 사항의 마지막 문장 끝에는 온점(.)을 찍지 않음
- 한 항목 안에 두 문장이 있을 때 첫 번째 문장에는 온점을 사용할 수 있으나, 마지막 문장에는 온점을 찍지 않음
- 제목, 표 항목, 담당자, 확인 필요 표시 뒤에도 문장 끝 온점을 붙이지 않음
- 약어, 소수점, 파일명, URL 등 의미상 필요한 온점은 유지

선택 용도별 작성 주안점:
{focus_lines}

사용자 보충 정보:
{metadata_block}

보충 정보 사용 원칙:
- `배경 지식/현장 메모`는 녹취록을 해석하기 위한 참고자료로 사용
- 녹취록에서 생략된 기관명, 약어, 배경 맥락, 현장 결정, 후속 작업을 보강
- 녹취록과 보충 정보가 충돌하면 단정하지 말고 `[확인 필요]`로 표시
- 보충 정보 자체를 별도 섹션으로 길게 옮기지 말고, 회의 목적/회의 내용/결정사항/향후 일정에 자연스럽게 반영
""".strip()


def _run_codex_job(job_id: str) -> None:
    with QUEUE_LOCK:
        with JOBS_LOCK:
            job = dict(JOBS[job_id])
        job_dir = Path(job["job_dir"])
        transcript_path = Path(job["transcript_path"])
        basename = job["basename"]
        formats = job["formats"]
        skill_mode = job.get("skill_mode", "official")
        profile = _skill_profile(skill_mode)
        skill_dir = Path(profile["skill_dir"])
        prompt = _build_prompt(job_dir, transcript_path, basename, formats, job.get("metadata", {}), skill_mode)
        prompt_path = job_dir / "prompt.txt"
        stdout_path = job_dir / "codex.stdout.log"
        stderr_path = job_dir / "codex.stderr.log"
        last_message_path = job_dir / "codex.last-message.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        _set_job(job_id, status="running", started_at=time.time(), message="Codex가 회의록을 생성하는 중입니다.")
        cmd = _codex_cmd(
            "--ask-for-approval",
            "never",
            "exec",
            "--cd",
            str(WORKSPACE_DIR),
            "--add-dir",
            str(job_dir),
            "--add-dir",
            str(skill_dir),
            "--sandbox",
            "danger-full-access",
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message_path),
            "-",
        )
        try:
            proc = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),
                capture_output=True,
                timeout=60 * 45,
                cwd=WORKSPACE_DIR,
            )
            stdout_path.write_bytes(proc.stdout or b"")
            stderr_path.write_bytes(proc.stderr or b"")
            sanitized_json = _sanitize_minutes_json(job_dir)
            postprocess = (
                _regenerate_outputs_from_minutes(job_dir, basename, formats, skill_dir)
                if sanitized_json
                else {"ok": False, "skipped": True}
            )
            renamed_outputs = _force_requested_output_names(job_dir, basename)
            files = _job_files(job_dir)
            ok_outputs = [item for item in files if str(item["name"]).lower().endswith((".hwpx", ".docx", ".pdf"))]
            downloads = _copy_outputs_to_downloads(job_id, job_dir) if ok_outputs else []
            status = "done" if proc.returncode == 0 and ok_outputs else "error"
            message = "완료되었습니다." if status == "done" else "Codex 실행은 끝났지만 결과 파일 확인이 필요합니다."
            error_detail = _job_error_detail(job_dir) if status == "error" else ""
            _set_job(
                job_id,
                status=status,
                finished_at=time.time(),
                returncode=proc.returncode,
                message=message,
                files=files,
                downloads=downloads,
                error_detail=error_detail,
                renamed_outputs=renamed_outputs,
                sanitized_json=sanitized_json,
                postprocess=postprocess,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "Codex job timed out.", encoding="utf-8")
            _set_job(
                job_id,
                status="error",
                finished_at=time.time(),
                message="45분 제한 시간을 초과했습니다.",
                files=_job_files(job_dir),
                error_detail=_job_error_detail(job_dir),
            )
        except Exception as exc:  # noqa: BLE001 - show local operator the failure.
            stderr_path.write_text(str(exc), encoding="utf-8")
            _set_job(
                job_id,
                status="error",
                finished_at=time.time(),
                message=f"실행 오류: {exc}",
                files=_job_files(job_dir),
                error_detail=_job_error_detail(job_dir),
            )


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexMinutesWeb/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            if not (STATIC_DIR / "index.html").exists() and (STATIC_DIR / "weekly-report.html").exists():
                return self._serve_file(STATIC_DIR / "weekly-report.html", "text/html; charset=utf-8")
            return self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if parsed.path == "/opinion-mvp":
            return self._serve_file(STATIC_DIR / "opinion-mvp.html", "text/html; charset=utf-8")
        if parsed.path == "/opinion-mvp-mobile":
            return self._serve_file(STATIC_DIR / "opinion-mvp-mobile.html", "text/html; charset=utf-8")
        if parsed.path == "/opinion-mvp-admin":
            return self._serve_file(STATIC_DIR / "opinion-mvp-admin.html", "text/html; charset=utf-8")
        if parsed.path == "/weekly-report":
            return self._serve_file(STATIC_DIR / "weekly-report.html", "text/html; charset=utf-8")
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if parsed.path == "/health":
            return _json_response(
                self,
                {
                    "ok": True,
                    "codex": CODEX_BIN,
                    "academy_weekly_report": {
                        "skill_path": str(ACADEMY_WEEKLY_SKILL_DIR),
                        "script_path": str(ACADEMY_WEEKLY_SCRIPT),
                        "exists": ACADEMY_WEEKLY_SCRIPT.exists(),
                    },
                    "skills": {
                        key: {
                            "label": profile["label"],
                            "name": profile["skill_name"],
                            "path": str(profile["skill_dir"]),
                            "exists": Path(profile["skill_dir"]).exists(),
                        }
                        for key, profile in SKILL_PROFILES.items()
                    },
                },
            )
        if parsed.path == "/api/codex/status":
            return self._handle_codex_status()
        if parsed.path == "/api/opinions":
            return self._handle_opinion_list(parsed)
        if parsed.path == "/api/opinions.csv":
            return self._handle_opinion_csv_download(parsed)
        if parsed.path.startswith("/api/opinions/"):
            return self._handle_opinion_detail(parsed)
        if parsed.path.startswith("/api/jobs/"):
            return self._handle_job_get(parsed)
        if parsed.path.startswith("/api/weekly/download-template"):
            return self._handle_weekly_template_download()
        if parsed.path.startswith("/static/"):
            candidate = (STATIC_DIR / parsed.path.removeprefix("/static/")).resolve()
            if STATIC_DIR.resolve() in candidate.parents and candidate.exists():
                content_type = "text/html; charset=utf-8" if candidate.suffix == ".html" else "application/octet-stream"
                return self._serve_file(candidate, content_type)
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            return self._handle_create_job()
        if parsed.path == "/api/weekly/analyze":
            return self._handle_weekly_analyze()
        if parsed.path == "/api/codex/login":
            return self._handle_codex_login()
        if parsed.path == "/api/opinion-admin/login":
            return self._handle_opinion_admin_login()
        if parsed.path == "/api/opinions":
            return self._handle_create_opinion()
        self.send_error(HTTPStatus.NOT_FOUND)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_create_job(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES:
            return _json_response(self, {"error": "업로드 용량이 너무 큽니다."}, 413)
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )

        transcript = (form.getfirst("transcript") or "").strip()
        upload = form["file"] if "file" in form else None
        if upload is not None and getattr(upload, "filename", ""):
            raw = upload.file.read()
            transcript = raw.decode("utf-8", errors="replace").strip()
        if not transcript:
            return _json_response(self, {"error": "녹취록 텍스트 또는 txt 파일이 필요합니다."}, 400)

        formats = ",".join(
            fmt for fmt in ["hwpx", "docx", "pdf"] if (form.getfirst(f"format_{fmt}") or "") == "on"
        ) or "hwpx"
        skill_mode = form.getfirst("skill_mode") or "official"
        profile = _skill_profile(skill_mode)
        if skill_mode not in SKILL_PROFILES:
            return _json_response(self, {"error": "지원하지 않는 회의록 용도입니다."}, 400)
        if not Path(profile["skill_dir"]).exists():
            return _json_response(
                self,
                {"error": f"{profile['label']} 스킬 폴더를 찾을 수 없습니다: {profile['skill_dir']}"},
                400,
            )
        basename = _safe_name(form.getfirst("basename") or DEFAULT_BASENAME)
        metadata = {
            "회의일시": form.getfirst("meeting_datetime") or "",
            "장소": form.getfirst("location") or "",
            "프로젝트명": form.getfirst("project_name") or "",
            "참석자": form.getfirst("attendees") or "",
            "배경 지식/현장 메모": form.getfirst("context_notes") or "",
        }

        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = job_dir / "transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")

        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": time.time(),
                "job_dir": str(job_dir),
                "transcript_path": str(transcript_path),
                "basename": basename,
                "formats": formats,
                "skill_mode": skill_mode,
                "skill_label": profile["label"],
                "skill_name": profile["skill_name"],
                "metadata": metadata,
                "message": "대기열에 등록되었습니다.",
                "files": [],
            }
        thread = threading.Thread(target=_run_codex_job, args=(job_id,), daemon=True)
        thread.start()
        _json_response(self, {"id": job_id, "status": "queued"})

    def _handle_weekly_template_download(self) -> None:
        template_candidates = [
            WORKSPACE_DIR / "outputs" / "weekly_input_template" / "주간_학부모리포트_입력양식_샘플.xlsx",
            APP_DIR.parent / "outputs" / "weekly_input_template" / "주간_학부모리포트_입력양식_샘플.xlsx",
        ]
        template = next((path for path in template_candidates if path.exists()), template_candidates[0])
        if not template.exists():
            _json_response(self, {"error": "weekly input template not found"}, HTTPStatus.NOT_FOUND)
            return
        body = template.read_bytes()
        encoded_name = quote(template.name)
        fallback_name = "weekly_parent_report_input_template.xlsx"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{fallback_name}"; filename*=UTF-8\'\'{encoded_name}',
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_weekly_analyze(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES:
            return _json_response(self, {"error": "업로드 용량이 너무 큽니다."}, 413)
        if not ACADEMY_WEEKLY_SCRIPT.exists():
            return _json_response(
                self,
                {"error": f"academy-weekly-report 스크립트를 찾을 수 없습니다: {ACADEMY_WEEKLY_SCRIPT}"},
                500,
            )
        python_bin = WORKSPACE_PYTHON if WORKSPACE_PYTHON.exists() else Path(sys.executable)
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )
        upload = form["file"] if "file" in form else None
        if upload is None or not getattr(upload, "filename", ""):
            return _json_response(self, {"error": "주간 입력 엑셀 파일이 필요합니다."}, 400)

        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / f"weekly-{job_id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = _safe_name(Path(upload.filename).name, "weekly-input.xlsx")
        input_path = job_dir / safe_filename
        input_path.write_bytes(upload.file.read())
        output_path = job_dir / "weekly-report-analysis.json"

        cmd = [
            str(python_bin),
            str(ACADEMY_WEEKLY_SCRIPT),
            str(input_path),
            str(output_path),
            "--academy",
            form.getfirst("academy") or "",
            "--report-title",
            form.getfirst("report_title") or "",
            "--period-type",
            form.getfirst("period_type") or "주간",
            "--period",
            form.getfirst("period") or "",
            "--class-name",
            form.getfirst("class_name") or "",
            "--course-name",
            form.getfirst("course_name") or "",
            "--course-type",
            form.getfirst("course_type") or "내신",
            "--teacher",
            form.getfirst("teacher") or "",
            "--test-name",
            form.getfirst("test_name") or "",
            "--test-date",
            form.getfirst("test_date") or "",
        ]
        proc = subprocess.run(
            cmd,
            cwd=WORKSPACE_DIR,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0 or not output_path.exists():
            return _json_response(
                self,
                {
                    "error": "주간 리포트 분석에 실패했습니다.",
                    "stdout": proc.stdout[-4000:],
                    "stderr": proc.stderr[-4000:],
                },
                500,
            )
        try:
            report = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return _json_response(self, {"error": f"분석 JSON을 읽지 못했습니다: {exc}"}, 500)

        with JOBS_LOCK:
            JOBS[f"weekly-{job_id}"] = {
                "id": f"weekly-{job_id}",
                "status": "done",
                "created_at": time.time(),
                "finished_at": time.time(),
                "job_dir": str(job_dir),
                "message": "주간 리포트 분석 완료",
                "files": _job_files(job_dir),
            }

        _json_response(
            self,
            {
                "ok": True,
                "jobId": job_id,
                "analysis": report,
                "downloadUrl": f"/api/jobs/weekly-{job_id}/download?file={output_path.name}",
            },
        )

    def _handle_codex_status(self) -> None:
        try:
            proc = subprocess.run(
                _codex_cmd("login", "status"),
                text=True,
                capture_output=True,
                timeout=10,
                cwd=APP_DIR,
            )
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            _json_response(
                self,
                {
                    "ok": proc.returncode == 0,
                    "logged_in": proc.returncode == 0 and "logged in" in output.lower(),
                    "message": output or "상태 메시지가 없습니다.",
                    "returncode": proc.returncode,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _json_response(
                self,
                {
                    "ok": False,
                    "logged_in": False,
                    "message": f"Codex 상태 확인 실패: {exc}",
                },
                500,
            )

    def _handle_codex_login(self) -> None:
        try:
            if os.name == "nt":
                command = f'cd /d "{APP_DIR}" && "{CODEX_BIN}" login && echo. && echo 로그인이 끝났으면 이 창을 닫아도 됩니다. && pause'
                subprocess.Popen(["cmd", "/c", "start", "Codex Login", "cmd", "/k", command], shell=False)
            elif _is_macos():
                command = f'cd "{APP_DIR}" && "{CODEX_BIN}" login; echo ""; echo "로그인이 끝났으면 이 창을 닫아도 됩니다."; read -k 1 -s "?아무 키나 누르면 닫습니다."'
                script = f'tell application "Terminal" to do script {json.dumps(command)}'
                subprocess.Popen(["osascript", "-e", script])
            else:
                command = f'cd "{APP_DIR}" && "{CODEX_BIN}" login; echo ""; echo "로그인이 끝났으면 이 창을 닫아도 됩니다."; read -r -p "Enter 키를 누르면 닫습니다."'
                subprocess.Popen(["sh", "-lc", command])
            _json_response(self, {"ok": True, "message": "Codex 로그인 터미널을 열었습니다."})
        except Exception as exc:  # noqa: BLE001
            _json_response(self, {"ok": False, "message": f"로그인 터미널 열기 실패: {exc}"}, 500)

    def _opinion_query_filters(self, parsed) -> dict[str, str]:
        query = parse_qs(parsed.query)
        return {
            "gender": (query.get("gender") or [""])[0],
            "age": (query.get("age") or [""])[0],
            "dong": (query.get("dong") or [""])[0],
            "keyword": (query.get("keyword") or [""])[0],
            "attachment": (query.get("attachment") or [""])[0],
        }

    def _handle_opinion_admin_login(self) -> None:
        payload = _read_json_body(self)
        admin_id = str(payload.get("id", "")).strip()
        password = str(payload.get("password", "")).strip()
        if admin_id == OPINION_ADMIN_ID and password == OPINION_ADMIN_PW:
            return _json_response(self, {"ok": True, "token": OPINION_ADMIN_TOKEN})
        return _json_response(self, {"ok": False, "error": "관리자 계정 정보가 올바르지 않습니다."}, 401)

    def _handle_create_opinion(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_OPINION_UPLOAD_BYTES:
            return _json_response(self, {"ok": False, "error": "전체 업로드 용량이 너무 큽니다."}, 413)
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )
        data = {
            "name": (form.getfirst("name") or "").strip(),
            "gender": (form.getfirst("gender") or "").strip(),
            "age": (form.getfirst("age") or "").strip(),
            "dong": (form.getfirst("dong") or "").strip(),
            "title": (form.getfirst("title") or "").strip(),
            "content": (form.getfirst("content") or "").strip(),
        }
        required = ["name", "gender", "age", "dong", "title", "content"]
        if any(not data[key] for key in required):
            return _json_response(self, {"ok": False, "error": "필수 항목을 모두 입력해 주세요."}, 400)
        if len(data["name"]) > 30 or not re.fullmatch(r"[가-힣A-Za-z\s]+", data["name"]):
            return _json_response(self, {"ok": False, "error": "이름은 한글 또는 영문 30자 이내로 입력해 주세요."}, 400)
        if len(data["title"]) > 50:
            return _json_response(self, {"ok": False, "error": "제목은 50자 이내로 입력해 주세요."}, 400)
        if len(data["content"]) > 1000:
            return _json_response(self, {"ok": False, "error": "의견 내용은 1000자 이내로 입력해 주세요."}, 400)

        OPINION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        items = _load_opinions()
        opinion_id = _next_opinion_id(items)
        attachments = []
        for field in ("photo1", "photo2"):
            upload = form[field] if field in form else None
            if upload is None or not getattr(upload, "filename", ""):
                continue
            raw = upload.file.read()
            if not raw:
                continue
            if len(raw) > MAX_OPINION_IMAGE_BYTES:
                return _json_response(self, {"ok": False, "error": "첨부 이미지는 파일당 5MB 이하만 업로드할 수 있습니다."}, 413)
            content_type = getattr(upload, "type", "") or ""
            if content_type and not content_type.startswith("image/"):
                return _json_response(self, {"ok": False, "error": "이미지 파일만 첨부할 수 있습니다."}, 400)
            original_name = _safe_upload_name(upload.filename, f"{field}.jpg")
            stored_name = f"{opinion_id}_{field}_{uuid.uuid4().hex[:8]}_{original_name}"
            (OPINION_UPLOAD_DIR / stored_name).write_bytes(raw)
            attachments.append(
                {
                    "field": field,
                    "filename": original_name,
                    "stored": stored_name,
                    "size": len(raw),
                }
            )

        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        item = {
            "id": opinion_id,
            "created_at": created_at,
            "created_label": time.strftime("%m.%d %H:%M"),
            "status": "검토 전",
            "attachments": attachments,
            **data,
        }
        items.append(item)
        _save_opinions(items)
        return _json_response(self, {"ok": True, "id": opinion_id, "created_at": created_at})

    def _filtered_opinions(self, parsed) -> list[dict]:
        filters = self._opinion_query_filters(parsed)
        items = sorted(_load_opinions(), key=lambda item: item.get("created_at", ""), reverse=True)
        return [item for item in items if _opinion_matches(item, filters)]

    def _handle_opinion_list(self, parsed) -> None:
        if not _is_opinion_admin(self):
            return _json_response(self, {"ok": False, "error": "관리자 로그인이 필요합니다."}, 401)
        all_items = _load_opinions()
        filtered = self._filtered_opinions(parsed)
        return _json_response(self, {"ok": True, "items": filtered, "stats": _opinion_stats(all_items), "count": len(filtered)})

    def _handle_opinion_detail(self, parsed) -> None:
        if not _is_opinion_admin(self):
            return _json_response(self, {"ok": False, "error": "관리자 로그인이 필요합니다."}, 401)
        opinion_id = parsed.path.rstrip("/").split("/")[-1]
        item = next((opinion for opinion in _load_opinions() if opinion.get("id") == opinion_id), None)
        if not item:
            return _json_response(self, {"ok": False, "error": "접수 건을 찾을 수 없습니다."}, 404)
        return _json_response(self, {"ok": True, "item": item})

    def _handle_opinion_csv_download(self, parsed) -> None:
        if not _is_opinion_admin(self):
            return _json_response(self, {"ok": False, "error": "관리자 로그인이 필요합니다."}, 401)
        body = _opinion_csv(self._filtered_opinions(parsed))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="mokpo-opinions.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_job_get(self, parsed) -> None:
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 3:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id = parts[2]
        with JOBS_LOCK:
            job = dict(JOBS.get(job_id) or {})
        if not job:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_dir = Path(job["job_dir"])
        if len(parts) == 4 and parts[3] == "download":
            query = parse_qs(parsed.query)
            filename = (query.get("file") or [""])[0]
            candidate = (job_dir / filename).resolve()
            if job_dir.resolve() not in candidate.parents or not candidate.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = candidate.read_bytes()
            fallback_name = candidate.name.encode("ascii", "ignore").decode("ascii") or "download"
            encoded_name = quote(candidate.name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{fallback_name}"; filename*=UTF-8\'\'{encoded_name}',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        job["files"] = _job_files(job_dir)
        public = {key: value for key, value in job.items() if key not in {"transcript_path"}}
        _json_response(self, public)


def main() -> int:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OPINION_DIR.mkdir(parents=True, exist_ok=True)
    OPINION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SKIP_CODEX_SKILL_CHECK", "0") != "1":
        missing_skills = [
            f"{profile['label']}({profile['skill_dir']})"
            for profile in SKILL_PROFILES.values()
            if not Path(profile["skill_dir"]).exists()
        ]
        if missing_skills:
            raise SystemExit(f"Skill directory not found: {', '.join(missing_skills)}")
        if not CODEX_BIN or not _codex_exists():
            raise SystemExit("Codex CLI not found.")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Codex minutes web app: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    if os.environ.get("OPEN_BROWSER", "1") != "0":
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
