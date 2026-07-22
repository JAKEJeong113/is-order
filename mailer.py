# mailer.py
"""Resend(https://resend.com) API로 트랜잭션 이메일(비밀번호 재설정 등)을 보낸다.
RESEND_API_KEY, RESEND_FROM_EMAIL 환경변수가 필요하다."""
import html
import os

import requests

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "i's ORDER <noreply@is-cream.co.kr>")
RESEND_API_URL = "https://api.resend.com/emails"


def send_email(to_email: str, subject: str, html_body: str) -> tuple[bool, str]:
    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY가 설정되지 않았습니다."

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": RESEND_FROM_EMAIL, "to": [to_email], "subject": subject, "html": html_body},
            timeout=10,
        )
    except requests.RequestException as e:
        return False, str(e)

    if resp.status_code >= 400:
        return False, f"{resp.status_code} {resp.text}"
    return True, "발송 완료"


def send_password_reset_email(to_email: str, reset_url: str) -> tuple[bool, str]:
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
      <h2 style="color:#14b8a6;">비밀번호 재설정</h2>
      <p>아래 버튼을 눌러 비밀번호를 재설정하세요. 이 링크는 30분간 유효합니다.</p>
      <p><a href="{reset_url}" style="display:inline-block; padding:12px 20px; background:#14b8a6;
         color:white; text-decoration:none; border-radius:8px; font-weight:700;">비밀번호 재설정</a></p>
      <p style="color:#6b7280; font-size:13px;">본인이 요청하지 않았다면 이 메일을 무시하셔도 됩니다.</p>
    </div>
    """
    return send_email(to_email, "[i's ORDER] 비밀번호 재설정", html)


def send_account_deleted_email(to_email: str, display_name: str, reason: str) -> tuple[bool, str]:
    body = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
      <h2 style="color:#dc2626;">계정 삭제 안내</h2>
      <p>{html.escape(display_name)}님, i's ORDER 계정이 관리자에 의해 삭제되었습니다.</p>
      <p style="background:#f3f4f6; padding:12px 16px; border-radius:8px; white-space:pre-wrap;">{html.escape(reason)}</p>
      <p style="color:#6b7280; font-size:13px;">문의사항이 있으시면 관리자에게 연락해주세요.</p>
    </div>
    """
    return send_email(to_email, "[i's ORDER] 계정이 삭제되었습니다", body)
