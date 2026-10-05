"""受控审计夹具，无外部服务、真实凭据或网络监听。"""

PUBLIC_DIAGNOSTIC = "CP3-controlled-diagnostic"


def handle(path, authorization=None):
    if path == "/public":
        return 200, PUBLIC_DIAGNOSTIC
    if path == "/admin":
        if authorization != "Bearer fixture-admin":
            return 403, "forbidden"
        return 200, "admin fixture"
    return 404, "not found"
