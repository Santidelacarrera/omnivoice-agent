"""El proveedor actual cumple el contrato del plano de control y el marcador solo usa ese contrato."""
import inspect

from app.telephony import dialer
from app.telephony.provider import TelephonyProvider
from app.telephony.twilio import TwilioClient

CONTRACT = [n for n, _ in inspect.getmembers(TelephonyProvider, inspect.isfunction) if not n.startswith("_")]


def test_twilio_client_and_test_double_implement_the_contract():
    assert set(CONTRACT) == {"create_call", "redirect_call", "hangup", "aclose"}
    for cls in (TwilioClient,):
        for name in CONTRACT:
            assert inspect.iscoroutinefunction(getattr(cls, name)), name
    assert isinstance(TwilioClient("AC1", "tok"), TelephonyProvider)


def test_dialer_only_calls_contract_methods_on_the_client():
    src = inspect.getsource(dialer)
    used = {m for m in ("create_call", "redirect_call", "hangup", "aclose") if f"client.{m}" in src}
    assert used <= set(CONTRACT) and "create_call" in used
