# Adapted from the PingAn http-server template.
# The stack requires Python >= 3.12 (see pyproject requires-python);
# the template's stock base image (py3.9) is NOT usable — pin the internal
# registry's py3.12 image here.
FROM hub-stg.paic.com.cn/pib-core/python:3.12-slim

RUN mkdir -p /root/.pip
COPY pip.conf /root/.pip/

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

RUN mkdir -p /mapi/logs /mapi/data
COPY api /mapi/api
COPY common /mapi/common
COPY config /mapi/config
COPY clause_splitter /mapi/clause_splitter
COPY tests /mapi/tests
# Fixed PingAn sidecar agent package — copied into the build context on the intranet.
COPY detector-python-agent-v3.2.0 /mapi/detector-python-agent-v3.2.0
COPY serving.py /mapi/
COPY wait-for-sidecar-ready.sh /mapi/
COPY docker_cmd.sh /mapi/

WORKDIR /mapi

CMD bash wait-for-sidecar-ready.sh && bash docker_cmd.sh
