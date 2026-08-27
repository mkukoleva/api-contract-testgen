```bash
docker build -f DockerFile -t schemathesis-svc .
docker run --rm -p 8080:8080 schemathesis-svc
```

```bash
curl -X POST "http://127.0.0.1:8080/test?base_url=http://127.0.0.1:9911" \
                                           -H "Content-Type: application/yaml" \
                                           --data-binary @prototype/tests/fixtures/demo_openapi.yaml
```