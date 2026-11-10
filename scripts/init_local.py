"""Create this project's isolated local databases; never alter existing databases."""

import asyncio

import asyncpg


async def main():
    connection = await asyncpg.connect("postgresql://soup@127.0.0.1:55432/postgres")
    try:
        for name in ("soup", "soup_test", "soup_chaos"):
            if not await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
                await connection.execute(f'CREATE DATABASE "{name}"')
                print(f"created {name}")
    finally:
        await connection.close()


asyncio.run(main())
