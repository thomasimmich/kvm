"""`cua` command line: observe, run a goal, single actions, or serve MCP + dashboard."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import get_settings
from .service import CuaService, screen_text


def _kv(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--input expects name=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cua", description="KVM computer-using agent")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--parser", choices=["omniparser", "ocr", "null"], help="override CUA_PARSER")
    p.add_argument(
        "--fake",
        action="append",
        type=Path,
        metavar="IMAGE_OR_DIR",
        help="no device: replay screenshots (repeatable; a dir means its *.png in name order). "
        "The next one appears after each click/scroll/key",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state", help="device and video state")

    o = sub.add_parser("observe", help="screenshot + parse, print the elements")
    o.add_argument("--save", type=Path, help="save the raw screenshot (PNG) and an annotated copy next to it")
    o.add_argument("--json", action="store_true", help="print elements as JSON")

    s = sub.add_parser("screenshot", help="save the current screen without parsing")
    s.add_argument("path", type=Path)

    r = sub.add_parser("run", help="reach a goal autonomously")
    r.add_argument("goal")
    r.add_argument("--hint", action="append", default=[], help="application knowledge; repeatable")
    r.add_argument(
        "--input", action="append", default=[], help="name=value text the agent may type; repeatable"
    )
    r.add_argument("--expect", help="stop as soon as this text is visible")
    r.add_argument("--max-steps", type=int)

    c = sub.add_parser("click", help="click the element whose label contains TEXT")
    c.add_argument("text")
    c.add_argument("--double", action="store_true")

    sc = sub.add_parser("scroll", help="scroll the wheel")
    sc.add_argument("direction", choices=["up", "down", "left", "right"])
    sc.add_argument("--notches", type=int, default=5)

    t = sub.add_parser("type", help="type text on the remote machine")
    t.add_argument("text")

    k = sub.add_parser("press", help="press a key combo, e.g. ctrl+l, enter, esc")
    k.add_argument("combo")

    m = sub.add_parser("move", help="move the mouse (screen pixels), human-like")
    m.add_argument("x", type=float)
    m.add_argument("y", type=float)

    sv = sub.add_parser("serve", help="MCP server (/mcp) + dashboard (/dashboard)")
    sv.add_argument("--host", help="lan (default: all interfaces, LAN address shown) or an address")
    sv.add_argument("--port", type=int)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    if args.parser:
        settings.cua_parser = args.parser
    device = None
    if args.fake:
        from .kvm.device import FakeKvm

        images = [f for p_ in args.fake for f in (sorted(p_.glob("*.png")) if p_.is_dir() else [p_])]
        device = FakeKvm(images)
    svc = CuaService(settings, device=device)

    if args.cmd == "state":
        if args.fake:
            raise SystemExit("state needs a real device")
        print(json.dumps(svc.device.state(), indent=2))  # type: ignore[attr-defined]
    elif args.cmd == "screenshot":
        frame = svc.device.screenshot()
        frame.image.save(args.path)
        print(f"saved {args.path} ({frame.image.size[0]}x{frame.image.size[1]}, seq {frame.seq})")
    elif args.cmd == "observe":
        scr = svc.observe()
        if args.save:
            from .perception.annotate import annotate

            scr.image.save(args.save)
            ann = args.save.with_suffix(".annotated.png")
            annotate(scr.image, scr.elements).save(ann)
            (args.save.with_suffix(".elements.json")).write_text(
                json.dumps([e.to_json() for e in scr.elements], ensure_ascii=False, indent=1)
            )
            print(f"saved {args.save}, {ann}", file=sys.stderr)
        if args.json:
            print(json.dumps([e.to_json() for e in scr.elements], ensure_ascii=False, indent=1))
        else:
            print(screen_text(scr))
    elif args.cmd == "run":
        res = svc.run_goal(args.goal, args.hint, _kv(args.input), args.max_steps, args.expect, source="cli")
        print(res.summary())
        sys.exit(0 if res.status == "done" else 1)
    elif args.cmd == "click":
        scr, msg = svc.click_text(args.text, args.double)
        print(msg)
        print(screen_text(scr, 3000))
    elif args.cmd == "scroll":
        print(screen_text(svc.scroll(args.direction, args.notches), 3000))
    elif args.cmd == "type":
        print(screen_text(svc.type_text(args.text), 3000))
    elif args.cmd == "press":
        print(screen_text(svc.press(args.combo), 3000))
    elif args.cmd == "move":
        svc.agent.actuator.move_to(args.x, args.y)
        print(f"moved to {args.x},{args.y}")
    elif args.cmd == "serve":
        from .mcp_server import serve

        if args.host:
            settings.cua_mcp_host = args.host
        if args.port:
            settings.cua_mcp_port = args.port
        serve(svc)


if __name__ == "__main__":
    main()
