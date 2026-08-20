VALID_SCHEDULE_ROUTES = {"evan", "gale"}


def normalize_schedule_route(route_to, default_route="evan"):
    route = str(route_to or default_route).strip().lower()
    if route not in VALID_SCHEDULE_ROUTES:
        raise ValueError("route_to 只能是 evan 或 gale")
    return route


def schedule_delivery_target(route_to, default_route, targets):
    route = normalize_schedule_route(route_to, default_route)
    url, secret = targets[route]
    return route, url, secret
