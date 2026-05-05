# test_token.py
import asyncio
from app.auth.token_manager import token_manager

async def main():
    await token_manager.authenticate()
    action = await token_manager.get_action_token()

    print("sessionToken:", token_manager.session_token)
    print("accessToken:", token_manager.access_token)
    print("actionToken:", action)

asyncio.run(main())