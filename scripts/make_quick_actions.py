#!/usr/bin/env python3
"""Build the Finder Quick Actions: "Copy to Phone" and "Move to Phone".

Right-click a file (or several) in Finder -> Quick Actions -> Copy to Phone, and
it lands in ~/FileBridge/to-phone ready for the app to collect.

A Quick Action is an Automator workflow bundle: a directory with an Info.plist
declaring the service, and a document.wflow describing one Run Shell Script
action. Both are plists, so they are generated with plistlib here rather than
typed out as XML — a single malformed key makes the action vanish from the menu
with no error anywhere, which is a miserable thing to debug.

    python3 make_quick_actions.py                 # install to ~/Library/Services
    python3 make_quick_actions.py /tmp/out        # build somewhere else

Nothing here talks to the server; it only puts files in the shared folder.
"""

import os
import plistlib
import shutil
import sys
import uuid

SERVICES = os.path.expanduser("~/Library/Services")

# The body of both actions. $@ is the list of selected files, because the action
# is configured to pass input "as arguments" rather than on stdin.
#
# Collisions are given a numbered name instead of overwriting: the folder is a
# staging area the phone reads from, and silently replacing a file someone is
# midway through collecting is not a trade worth making.
SCRIPT = r'''
# The served folder is a command-line argument, not a constant, so the server
# records where it is. Falling back to the default keeps the action working
# before the app has ever been started.
ROOT="$(cat "$HOME/.filebridge/root" 2>/dev/null)"
[ -d "$ROOT" ] || ROOT="$HOME/FileBridge"
DEST="$ROOT/to-phone"
mkdir -p "$DEST" || exit 1

moved=0
skipped=0

for src in "$@"; do
  base="$(basename "$src")"

  # Anything already inside the bridge is left alone. Moving a file onto itself
  # is how you lose it.
  case "$src" in
    "$ROOT"/*) skipped=$((skipped + 1)); continue ;;
  esac

  target="$DEST/$base"
  if [ -e "$target" ]; then
    stem="${base%.*}"
    ext="${base##*.}"
    if [ "$stem" = "$base" ]; then ext=""; fi
    n=2
    while [ -e "$target" ] && [ "$n" -lt 100 ]; do
      if [ -n "$ext" ]; then
        target="$DEST/$stem-$n.$ext"
      else
        target="$DEST/$base-$n"
      fi
      n=$((n + 1))
    done
  fi

  if __VERB__ "$src" "$target"; then
    moved=$((moved + 1))
  else
    skipped=$((skipped + 1))
  fi
done

note="$moved to to-phone"
if [ "$skipped" -gt 0 ]; then note="$note, $skipped skipped"; fi
osascript -e "display notification \"$note\" with title \"File Bridge\"" >/dev/null 2>&1
'''


def shell_script(verb):
    return SCRIPT.replace("__VERB__", verb).strip() + "\n"


def info_plist(name):
    """Declares the service so Finder shows it for any file or folder."""
    return {
        "NSServices": [{
            "NSMenuItem": {"default": name},
            "NSMessage": "runWorkflowAsService",
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": ["public.item"],
        }],
    }


def document_wflow(script):
    """One Run Shell Script action, fed the selection as arguments."""
    action_uuid = str(uuid.uuid4()).upper()
    input_uuid = str(uuid.uuid4()).upper()
    output_uuid = str(uuid.uuid4()).upper()

    return {
        "AMApplicationBuild": "528",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{
            "action": {
                "AMAccepts": {
                    "Container": "List",
                    "Optional": True,
                    "Types": ["com.apple.cocoa.string"],
                },
                "AMActionVersion": "2.0.3",
                "AMApplication": ["Automator"],
                "AMParameterProperties": {
                    "COMMAND_STRING": {},
                    "CheckedForUserDefaultShell": {},
                    "inputMethod": {},
                    "shell": {},
                    "source": {},
                },
                "AMProvides": {
                    "Container": "List",
                    "Types": ["com.apple.cocoa.string"],
                },
                "ActionBundlePath":
                    "/System/Library/Automator/Run Shell Script.action",
                "ActionName": "Run Shell Script",
                "ActionParameters": {
                    "COMMAND_STRING": script,
                    "CheckedForUserDefaultShell": True,
                    # 1 = pass the input as arguments ("$@"), not on stdin.
                    "inputMethod": 1,
                    "shell": "/bin/zsh",
                    "source": "",
                },
                "BundleIdentifier": "com.apple.RunShellScript",
                "CFBundleVersion": "2.0.3",
                "CanShowSelectedItemsWhenRun": False,
                "CanShowWhenRun": True,
                "Category": ["AMCategoryUtilities"],
                "Class Name": "RunShellScriptAction",
                "InputUUID": input_uuid,
                "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
                "OutputUUID": output_uuid,
                "UUID": action_uuid,
                "UnlocalizedApplications": ["Automator"],
                "arguments": {
                    "0": {
                        "default value": 0,
                        "name": "inputMethod",
                        "required": "0",
                        "type": "0",
                        "uuid": "0",
                    },
                    "1": {
                        "default value": False,
                        "name": "CheckedForUserDefaultShell",
                        "required": "0",
                        "type": "0",
                        "uuid": "1",
                    },
                    "2": {
                        "default value": "",
                        "name": "source",
                        "required": "0",
                        "type": "0",
                        "uuid": "2",
                    },
                    "3": {
                        "default value": "",
                        "name": "COMMAND_STRING",
                        "required": "0",
                        "type": "0",
                        "uuid": "3",
                    },
                    "4": {
                        "default value": "/bin/sh",
                        "name": "shell",
                        "required": "0",
                        "type": "0",
                        "uuid": "4",
                    },
                },
                "isViewVisible": 1,
                "location": "309.000000:253.000000",
                "nibPath": "/System/Library/Automator/Run Shell Script.action"
                           "/Contents/Resources/Base.lproj/main.nib",
            },
            "isViewVisible": 1,
        }],
        "connectors": {},
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier":
                "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def build(name, verb, dest_dir):
    bundle = os.path.join(dest_dir, name + ".workflow")
    if os.path.exists(bundle):
        shutil.rmtree(bundle)
    contents = os.path.join(bundle, "Contents")
    os.makedirs(contents)

    with open(os.path.join(contents, "Info.plist"), "wb") as handle:
        plistlib.dump(info_plist(name), handle)
    with open(os.path.join(contents, "document.wflow"), "wb") as handle:
        plistlib.dump(document_wflow(shell_script(verb)), handle)

    return bundle


ACTIONS = (("Copy to Phone", "/bin/cp -R"), ("Move to Phone", "/bin/mv"))


def uninstall():
    """Take them back out. Anything the app adds to a user's system needs this."""
    for name, _ in ACTIONS:
        bundle = os.path.join(SERVICES, name + ".workflow")
        if os.path.isdir(bundle):
            shutil.rmtree(bundle)
            print("removed", bundle)
    stamp = os.path.expanduser("~/.filebridge/quick-actions")
    if os.path.exists(stamp):
        os.remove(stamp)


def main():
    if "--uninstall" in sys.argv:
        uninstall()
        return

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dest_dir = args[0] if args else SERVICES
    os.makedirs(dest_dir, exist_ok=True)

    for name, verb in ACTIONS:
        print("built", build(name, verb, dest_dir))

    if dest_dir == SERVICES:
        print("\nRight-click a file in Finder -> Quick Actions.")
        print("If they do not appear, they can be switched on in")
        print("System Settings -> Keyboard -> Keyboard Shortcuts -> Services.")
        print("Remove them again with: make_quick_actions.py --uninstall")


if __name__ == "__main__":
    main()
