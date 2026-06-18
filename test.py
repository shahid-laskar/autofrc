ERROR:    Exception in ASGI application
frc_recharge  | Traceback (most recent call last):
frc_recharge  |   File "/app/app/db/postgres.py", line 26, in wrapper
frc_recharge  |     return fn(*args, **kwargs)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/app/app/db/postgres.py", line 383, in find_row_by_pyro_trans_id
frc_recharge  |     cur.execute(sql, (pyro_trans_id,))
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/psycopg2/extras.py", line 236, in execute
frc_recharge  |     return super().execute(query, vars)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  | psycopg2.OperationalError: server closed the connection unexpectedly
frc_recharge  |         This probably means the server terminated abnormally
frc_recharge  |         before or while processing the request.
frc_recharge  |
frc_recharge  |
frc_recharge  | During handling of the above exception, another exception occurred:
frc_recharge  |
frc_recharge  | Traceback (most recent call last):
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/uvicorn/protocols/http/httptools_impl.py", line 421, in run_asgi
frc_recharge  |     result = await app(  # type: ignore[func-returns-value]
frc_recharge  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/uvicorn/middleware/proxy_headers.py", line 56, in __call__
frc_recharge  |     return await self.app(scope, receive, send)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/fastapi/applications.py", line 1159, in __call__
frc_recharge  |     await super().__call__(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/applications.py", line 90, in __call__
frc_recharge  |     await self.middleware_stack(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/middleware/errors.py", line 186, in __call__
frc_recharge  |     raise exc
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/middleware/errors.py", line 164, in __call__
frc_recharge  |     await self.app(scope, receive, _send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/middleware/exceptions.py", line 63, in __call__
frc_recharge  |     await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app
frc_recharge  |     raise exc
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
frc_recharge  |     await app(scope, receive, sender)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py", line 18, in __call__
frc_recharge  |     await self.app(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 660, in __call__
frc_recharge  |     await self.middleware_stack(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 680, in app
frc_recharge  |     await route.handle(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 276, in handle
frc_recharge  |     await self.app(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/fastapi/routing.py", line 134, in app
frc_recharge  |     await wrap_app_handling_exceptions(app, request)(scope, receive, send)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app
frc_recharge  |     raise exc
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
frc_recharge  |     await app(scope, receive, sender)
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/fastapi/routing.py", line 120, in app
frc_recharge  |     response = await f(request)
frc_recharge  |                ^^^^^^^^^^^^^^^^
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/fastapi/routing.py", line 674, in app
frc_recharge  |     raw_response = await run_endpoint_function(
frc_recharge  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/fastapi/routing.py", line 328, in run_endpoint_function
frc_recharge  |     return await dependant.call(**values)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/app/app/callback.py", line 65, in recharge_callback
frc_recharge  |     row = await async_find_row_by_pyro_trans_id(pyro_txn_id_int)
frc_recharge  |           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/app/app/db/postgres.py", line 443, in async_find_row_by_pyro_trans_id
frc_recharge  |     return await asyncio.to_thread(find_row_by_pyro_trans_id, pyro_trans_id)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/usr/local/lib/python3.12/asyncio/threads.py", line 25, in to_thread
frc_recharge  |     return await loop.run_in_executor(None, func_call)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/usr/local/lib/python3.12/concurrent/futures/thread.py", line 59, in run
frc_recharge  |     result = self.fn(*self.args, **self.kwargs)
frc_recharge  |              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/app/app/db/postgres.py", line 32, in wrapper
frc_recharge  |     return fn(*args, **kwargs)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^
frc_recharge  |   File "/app/app/db/postgres.py", line 383, in find_row_by_pyro_trans_id
frc_recharge  |     cur.execute(sql, (pyro_trans_id,))
frc_recharge  |   File "/usr/local/lib/python3.12/site-packages/psycopg2/extras.py", line 236, in execute
frc_recharge  |     return super().execute(query, vars)
frc_recharge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
frc_recharge  | psycopg2.OperationalError: server closed the connection unexpectedly
frc_recharge  |         This probably means the server terminated abnormally
frc_recharge  |         before or while processing the request.
frc_recharge  |
