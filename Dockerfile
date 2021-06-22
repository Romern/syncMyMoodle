FROM python:3.9-alpine

RUN touch /config.json

RUN adduser --disabled-password --home=/app syncmymoodle
COPY --chown=syncmymoodle:syncmymoodle . /app

USER syncmymoodle
WORKDIR /app

RUN pip install -r requirements.txt

ENTRYPOINT ["python", "syncMyMoodle.py", "--basedir=/syncBaseDir", "--config=/config.json"]
