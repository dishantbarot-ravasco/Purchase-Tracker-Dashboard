"""
apps/services/email_service.py — Shared content builder for every outgoing
email in the app (device-login OTP, new-device notifications, admin alerts).

Ported from the TDS Automation App's apps/services/email_service.py
(unchanged design, Purchase Tracker branding). This module only builds
(html_body, text_body). It never calls send_mail() itself and knows nothing
about SMTP configuration, fail_silently policy, or per-caller dev-mode
console fallbacks - each sender in apps/services/device_service.py keeps its
own send_mail() call and its own error handling; only the content
construction is centralized here so every email shares one consistent,
formal layout.

Every caller-supplied string (greeting/body_paragraphs/highlight_value/
after_highlight_paragraphs/closing/signature) is HTML-escaped before being
interpolated into html_body. Some of these values ultimately derive from a
request's User-Agent header (apps/services/device_service.py's
_get_device_name()), which is attacker-controlled - escaping keeps a crafted
User-Agent from injecting markup into the "new device" notification/admin
alert emails, whether or not the current send_mail() calls pass html_message
(they don't today - only the plain-text body is actually sent - but this
builder's job is to hand back a safe HTML body regardless of how a caller
uses it).
"""
import html


def render_email(
    greeting: str,
    body_paragraphs: list,
    highlight_value: str = None,
    highlight_label: str = "One-Time Password",
    after_highlight_paragraphs: list = None,
    closing: str = "Regards,",
    signature: str = "Ravasco Transmission and Packing Pvt Ltd.",
) -> tuple:
    """Build (html_body, text_body) for a formal, consistently-branded email."""
    after_highlight_paragraphs = after_highlight_paragraphs or []

    def _paragraphs_html(paragraphs, small=False):
        style = (
            "margin:0 0 12px;font-size:12px;color:#718096;line-height:1.5;" if small
            else "margin:0 0 16px;font-size:13px;color:#4A5568;line-height:1.6;"
        )
        return "".join(f'<p style="{style}">{html.escape(p)}</p>' for p in paragraphs)

    paragraphs_html = _paragraphs_html(body_paragraphs)
    after_highlight_html = _paragraphs_html(after_highlight_paragraphs, small=True)

    highlight_html = ""
    if highlight_value:
        highlight_html = (
            f'<p style="margin:0 0 20px;font-size:13px;color:#1A202C;">'
            f"{html.escape(highlight_label)}: "
            f'<span style="font-weight:700;letter-spacing:.05em;">{html.escape(highlight_value)}</span></p>'
        )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:16px;background:#FFFFFF;font-family:Arial,Helvetica,sans-serif;">
  <p style="margin:0 0 20px;font-size:13px;color:#1A202C;">{html.escape(greeting)}</p>
  {paragraphs_html}
  {highlight_html}
  {after_highlight_html}
  <p style="margin:20px 0 0;font-size:13px;color:#1A202C;line-height:1.6;">
    {html.escape(closing)}<br />{html.escape(signature)}
  </p>
  <p style="margin:24px 0 0;font-size:11px;color:#718096;">
    This is a system generated email. Please do not reply.
  </p>
</body>
</html>"""

    text_lines = [greeting, ""]
    for i, p in enumerate(body_paragraphs):
        if i > 0:
            text_lines.append("")
        text_lines.append(p)
    if highlight_value:
        text_lines.append("")
        text_lines.append(f"{highlight_label}: {highlight_value}")
    if after_highlight_paragraphs:
        text_lines.append("")
        text_lines.extend(p for p in after_highlight_paragraphs)
    text_lines.append("")
    text_lines.append(closing)
    text_lines.append(signature)
    text_lines.append("")
    text_lines.append("This is a system generated email. Please do not reply.")
    text_body = "\n".join(text_lines) + "\n"

    return html_body, text_body
