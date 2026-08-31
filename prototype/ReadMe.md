### install
```bash
cd prototype
touch .env
uv venv
source .venv/bin/activate.fish # source .venv/bin/activate
uv sync
uv run tester src/prototype/tests/fixtures/demo_openapi.yaml
```
### env-шаблон
```
DEEPCODE_API_KEY=<api-key>
DEEPCODE_BASE_URL=https://deepcode.ci.nsu.ru/api/v1
DEEPCODE_MODEL=deepseek-ai/DeepSeek-V4-Flash

```
### запуск микросервисов для тестирования 
```bash
cd prototype/src/prototype/service_tools
docker compose up -d
```
### цепочка вызова инстурументов
- schemathesis_tool
- demo_api_test_tool
- generate_user_story_tool
- verify_user_story_tool
- написать отчёт по результатам в prototype/src/prototype/reports