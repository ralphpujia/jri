from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.config import (
    X402_FACILITATOR_URL,
    X402_NETWORK,
    X402_PAY_TO_ADDRESS,
    X402_RALPH_PRICE_USD,
)

@dataclass(frozen=True)
class X402Authorization:
    headers: dict[str, str]
    payer: str | None
    network: str | None
    transaction: str | None


@lru_cache(maxsize=1)
def _load_x402_components() -> tuple[Any, Any, Any, Any, Any, Any, Any, Any] | None:
    try:
        http_module = import_module("x402.http")
        fastapi_middleware_module = import_module("x402.http.middleware.fastapi")
        http_types_module = import_module("x402.http.types")
        http_server_module = import_module("x402.http.x402_http_server")
        evm_server_module = import_module("x402.mechanisms.evm.exact.server")
        server_module = import_module("x402.server")
    except ImportError:
        return None

    return (
        server_module.x402ResourceServer,
        http_module.FacilitatorConfig,
        http_module.HTTPFacilitatorClient,
        http_types_module.HTTPRequestContext,
        http_types_module.PaywallConfig,
        fastapi_middleware_module.FastAPIAdapter,
        http_server_module.x402HTTPResourceServer,
        evm_server_module.ExactEvmScheme,
    )


def x402_enabled() -> bool:
    return bool(X402_PAY_TO_ADDRESS and _load_x402_components() is not None)


def _require_x402_components() -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    components = _load_x402_components()
    if not X402_PAY_TO_ADDRESS or components is None:
        raise RuntimeError("x402 is not configured")
    return components


@lru_cache(maxsize=1)
def _build_http_server():
    (
        resource_server_cls,
        facilitator_config_cls,
        facilitator_client_cls,
        _,
        _,
        _,
        http_resource_server_cls,
        exact_evm_scheme_cls,
    ) = _require_x402_components()

    facilitator = facilitator_client_cls(facilitator_config_cls(url=X402_FACILITATOR_URL))
    server = resource_server_cls(facilitator)
    server.register(X402_NETWORK, exact_evm_scheme_cls())

    routes = {
        "GET /api/projects/*/ralph/start-x402": {
            "accepts": {
                "scheme": "exact",
                "payTo": X402_PAY_TO_ADDRESS,
                "price": X402_RALPH_PRICE_USD,
                "network": X402_NETWORK,
            },
            "description": "Start a Just Ralph It build",
            "mimeType": "application/json",
        },
        "POST /api/projects/*/ralph/start-x402": {
            "accepts": {
                "scheme": "exact",
                "payTo": X402_PAY_TO_ADDRESS,
                "price": X402_RALPH_PRICE_USD,
                "network": X402_NETWORK,
            },
            "description": "Start a Just Ralph It build",
            "mimeType": "application/json",
        },
    }

    return http_resource_server_cls(server, routes)


_INITIALIZED = False


def _paywall_config(request: Request):
    (_, _, _, _, paywall_config_cls, _, _, _) = _require_x402_components()
    return paywall_config_cls(
        app_name="Just Ralph It",
        current_url=str(request.url),
        testnet=X402_NETWORK != "eip155:8453",
    )


def _instruction_response(status: int, headers: dict[str, str], body, is_html: bool) -> Response:
    if is_html:
        return HTMLResponse(content=body, status_code=status, headers=headers)
    return JSONResponse(content=body or {}, status_code=status, headers=headers)


async def authorize_x402_payment(request: Request) -> X402Authorization | Response:
    if not x402_enabled():
        return JSONResponse(
            content={"detail": "x402 is not configured for this deployment"},
            status_code=503,
        )

    (_, _, _, http_request_context_cls, _, fastapi_adapter_cls, _, _) = _require_x402_components()

    global _INITIALIZED
    http_server = _build_http_server()

    if not _INITIALIZED:
        try:
            http_server.initialize()
        except Exception as exc:
            return JSONResponse(
                content={"detail": f"x402 initialization failed: {exc}"},
                status_code=502,
            )
        _INITIALIZED = True

    adapter = fastapi_adapter_cls(request)
    context = http_request_context_cls(
        adapter=adapter,
        path=request.url.path,
        method=request.method,
        payment_header=adapter.get_header("payment-signature") or adapter.get_header("x-payment"),
    )

    try:
        result = await http_server.process_http_request(context, _paywall_config(request))
    except Exception as exc:
        return JSONResponse(
            content={"detail": f"x402 payment processing failed: {exc}"},
            status_code=502,
        )

    if result.type == "payment-error":
        response = result.response
        if response is None:
            return JSONResponse(content={"detail": "Payment required"}, status_code=402)
        return _instruction_response(
            status=response.status,
            headers=response.headers,
            body=response.body,
            is_html=response.is_html,
        )

    if result.type != "payment-verified":
        return JSONResponse(content={"detail": "x402 route misconfigured"}, status_code=500)

    try:
        settle_result = await http_server.process_settlement(
            result.payment_payload,
            result.payment_requirements,
            context=context,
        )
    except Exception as exc:
        return JSONResponse(
            content={"detail": f"x402 settlement failed: {exc}"},
            status_code=502,
        )
    if not settle_result.success:
        response = settle_result.response
        if response is None:
            return JSONResponse(content={}, status_code=402, headers=settle_result.headers)
        return _instruction_response(
            status=response.status,
            headers=response.headers,
            body=response.body,
            is_html=response.is_html,
        )

    return X402Authorization(
        headers=settle_result.headers,
        payer=settle_result.payer,
        network=settle_result.network,
        transaction=settle_result.transaction,
    )
