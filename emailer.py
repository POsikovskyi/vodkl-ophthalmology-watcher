import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email(smtp_cfg: dict, to_addr: str, subject: str, text_body: str,
                html_body: str | None = None,
                attachments: list[tuple[str, bytes, str]] | None = None):
    """attachments: list of (filename, content_bytes, mime_subtype e.g. 'text/html')"""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_addr"]
    msg["To"] = to_addr

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)

    for filename, content, mimetype in (attachments or []):
        maintype, _, subtype = mimetype.partition("/")
        part = MIMEApplication(content, _subtype=subtype or "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=30) as server:
        if smtp_cfg.get("use_tls", True):
            server.starttls()
        server.login(smtp_cfg["username"], smtp_cfg["password"])
        server.sendmail(smtp_cfg["from_addr"], [to_addr], msg.as_string())
