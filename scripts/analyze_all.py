import sys
import time
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from crypto_desk.config import load_settings
from crypto_desk.cli import _service, _publish_dashboard_if_configured

settings = load_settings(Path("config.yaml"))
service = _service(settings)

print(f"=== BẮT ĐẦU PHÂN TÍCH TẤT CẢ CÁC ĐỒNG ===", flush=True)
print(f"Danh sách: {list(settings.symbols)}", flush=True)
print(f"Model: {settings.models.quick} (Provider: {settings.models.provider})", flush=True)

results = {}
for i, symbol in enumerate(settings.symbols, 1):
    t0 = time.time()
    print(f"\n[{i}/{len(settings.symbols)}] Đang phân tích {symbol}...", flush=True)
    
    run = None
    for try_num in range(1, 3):
        try:
            run = service.analyze(symbol)
            # If conviction is 0 (fallback due to rate limit/data), try again once after 30s
            if run.decision.conviction == Decimal("0") and try_num == 1:
                print(f"--> [{symbol}] Conviction = 0 (có thể do nghẽn mạng/quota), đợi 30s thử lại lần 2...", flush=True)
                time.sleep(30)
                continue
            break
        except Exception as exc:
            print(f"--> [{symbol}] LỖI (lần {try_num}): {exc}", flush=True)
            if try_num == 1:
                print(f"--> [{symbol}] Đợi 30s thử lại...", flush=True)
                time.sleep(30)
            else:
                results[symbol] = {"status": "ERROR", "error": str(exc)}

    if run is not None:
        elapsed = round(time.time() - t0, 1)
        results[symbol] = {
            "status": "SUCCESS",
            "action": run.decision.action,
            "conviction": str(run.decision.conviction),
            "futures_bias": run.decision.futures_bias,
            "setups_count": len(run.decision.futures_setups),
            "setups": [
                f"{s.direction} (Entry: {s.entry}, SL: {s.stop}, TP: {s.target}, RR: {s.risk_reward_ratio})"
                for s in run.decision.futures_setups
            ],
            "elapsed": f"{elapsed}s",
        }
        print(
            f"--> [{symbol}] XONG trong {elapsed}s: Quyết định={run.decision.action} "
            f"({run.decision.conviction}/10) | Futures Bias={run.decision.futures_bias} | "
            f"Setups={len(run.decision.futures_setups)}",
            flush=True,
        )
        try:
            _publish_dashboard_if_configured(settings)
        except Exception as dash_exc:
            print(f"--> Cập nhật dashboard cảnh báo: {dash_exc}", flush=True)

    if i < len(settings.symbols):
        print("Nghỉ 10s trước khi sang đồng tiếp theo để giữ ổn định quota...", flush=True)
        time.sleep(10)

print("\n" + "=" * 50, flush=True)
print("TỔNG KẾT PHÂN TÍCH TẤT CẢ CÁC ĐỒNG:", flush=True)
for sym, info in results.items():
    if info.get("status") == "SUCCESS":
        print(f"• {sym}: {info['action']} (Conviction: {info['conviction']}/10) - Bias: {info['futures_bias']} - Setups: {info['setups_count']}", flush=True)
    else:
        print(f"• {sym}: LỖI ({info.get('error')})", flush=True)

_publish_dashboard_if_configured(settings)
print("\nĐã cập nhật toàn bộ dữ liệu lên Dashboard!", flush=True)
