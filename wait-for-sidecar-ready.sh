while [[ "$(curl -s -o /dev/null -w ''%{http_code}'' localhost:15020/healthz/ready)" != "200" ]];
do echo Waiting for Sidecar;
sleep 1;
done;
echo Sidecar available;
