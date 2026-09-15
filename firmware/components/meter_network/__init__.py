"""Private IPv4 LAN meter client with a bounded background request."""
import ipaddress
from urllib.parse import urlsplit

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import display, touchscreen
from esphome.const import CONF_ID, CONF_DISPLAY

DEPENDENCIES = ["esp32", "wifi", "display"]
namespace = cg.esphome_ns.namespace("meter_network")
MeterNetwork = namespace.class_("MeterNetwork", cg.Component)


def validate_settings(config):
    try:
        url = urlsplit(config["url"])
        address = ipaddress.IPv4Address(url.hostname)
        if (url.scheme != "http" or url.port is None or not 1024 <= url.port <= 65535
                or url.path != "/v2/usage" or url.query or url.fragment or url.username
                or url.password or address.is_unspecified or address.is_multicast):
            raise ValueError
    except (ValueError, TypeError):
        raise cv.Invalid("Use http://<Mac IPv4 address>:<port>/v2/usage (port 1024–65535).") from None
    token = config["token"]
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise cv.Invalid("Use a dedicated 64-character lowercase hex meter token.")
    poll = config["poll_interval"].total_milliseconds
    timeout = config["timeout"].total_milliseconds
    if poll % 1000 or not 10000 <= poll <= 300000 or timeout % 1000 or not 1000 <= timeout <= 30000 or timeout > poll:
        raise cv.Invalid("Use whole seconds: poll 10–300, timeout 1–30 and at most poll.")
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(MeterNetwork),
    cv.Required(CONF_DISPLAY): cv.use_id(display.Display),
    cv.Required("touch_calibration"): cv.All(cv.Schema({
        cv.Required(key): cv.int_range(min=0, max=4095)
        for key in ("x_min", "x_max", "y_min", "y_max")
    }), touchscreen.validate_calibration),
    cv.Optional("touch_transform", default={}): cv.Schema({
        cv.Optional(key, default=False): cv.boolean
        for key in ("swap_xy", "mirror_x", "mirror_y")
    }),
    cv.Required("url"): cv.string_strict,
    cv.Required("token"): cv.string_strict,
    cv.Optional("poll_interval", default="60s"): cv.positive_time_period_milliseconds,
    cv.Optional("timeout", default="10s"): cv.positive_time_period_milliseconds,
}).extend(cv.COMPONENT_SCHEMA), validate_settings)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_display(await cg.get_variable(config[CONF_DISPLAY])))
    cal, transform = config["touch_calibration"], config["touch_transform"]
    cg.add(var.set_touch_calibration(cal["x_min"], cal["x_max"], cal["y_min"], cal["y_max"],
                                    transform["swap_xy"], transform["mirror_x"], transform["mirror_y"]))
    url = urlsplit(config["url"])
    cg.add(var.configure(url.hostname, url.port, config["token"],
                         config["poll_interval"].total_milliseconds,
                         config["timeout"].total_milliseconds))
