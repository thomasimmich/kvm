import { keyboards } from "@/keyboardLayouts";
import { keys, modifiers } from "@/keyboardMappings";
const layouts: Record<string, unknown> = {};
for (const k of keyboards) layouts[k.isoCode] = { name: k.name, chars: k.chars };
console.log(JSON.stringify({ source: "generated from ui/src/keyboardLayouts by cua/scripts/export_keymaps.sh", keys, modifiers, layouts }));
