FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# Dependencies are installed only while building; execution never uses pip.
RUN python -m pip install --no-cache-dir \
    pytest==9.1.1 iniconfig==2.3.0 packaging==26.3 pluggy==1.6.0 pygments==2.21.0
COPY pytest_worker.py /opt/runner/pytest_worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HOME=/tmp
USER 65534:65534
WORKDIR /tests
ENTRYPOINT ["python", "-I", "-B", "/opt/runner/pytest_worker.py", "/tests", "/results/events.jsonl"]
