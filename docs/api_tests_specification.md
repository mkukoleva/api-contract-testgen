# Спецификация тестов API

**API:** JSONPlaceholder (https://jsonplaceholder.typicode.com)

## 7 тестов для проверки

1. GET /posts/1 → 200 + поле "title"
2. GET /users → 200 + поле "name"  
3. GET /posts/999999 → 404
4. POST /posts → 201 + поле "id"
5. GET /users/1 → 200 + id=1 + name="Leanne Graham"
6. GET /posts?userId=1 → 200 + все с userId=1
7. GET /posts/1/comments → 200 + postId=1

## Задание

Сгенерируй тесты для своего LLM-инструмента, которые проверяют все 7 пунктов.
Запиши результаты (PASS/FAIL) и отправь обратно.
