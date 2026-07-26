# bullseye (glibc 2.31) not bookworm: Docker 19.03's seccomp profile blocks glibc>=2.34's
# clone3() syscall -> "can't start new thread". ponytail: pin bullseye until Docker is
# upgraded to a build whose default seccomp allows clone3, then any base works.
FROM python:3.11-slim-bullseye
ENV PIP_PROGRESS_BAR=off PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_NO_INPUT=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
