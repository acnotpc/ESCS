FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY switchboard_bridge.py .
COPY switchboard_popups.py .
COPY mcp_server.py .
COPY recruitment_mail.py .
COPY recruitment_checklist_access.py .
COPY checklist_connection_monitor.py .
COPY hr_mail_readiness.py .
COPY hr_sender_test.py .
COPY recruitment_checklist.py .
COPY recruitment_receipts.py .
COPY recruitment_tracking.py .
COPY checklist_workflow.py .
COPY automatic_followup.py .
COPY reconciliation_notices.py .
COPY licence_expiry.py .
COPY history_email_example.py .
COPY history_completeness.py .
COPY isolated_followup_trial.py .
COPY reference_chasing.py .
COPY reference_chasing_adapters.py .
COPY reference_chasing_service.py .
COPY team_sync.py .
COPY daily_teams.py .
COPY fms_xlsx.py .
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
