# CLI Dungeon
 A small procedurally generated text adventure style game to practice CLI use.

 Run from a POSIX compliant terminal with write access:
 
 `source <(curl -s https://raw.githubusercontent.com/paralinguist/CLI-Dungeon/main/generate_dungeon.sh)`

 If you'd prefer not to execute some stranger's code remotely, directly from your terminal, download the generate_dungeon.sh file, make it executable and run.

 The generated dungeon can be found in the cli_dungeon folder.
 The instructions found in the guard.txt file are as follows:

---

Hello there, Command Wizard!
Thank goodness you made it - Goblins have overrun our keep and are trying to steal our treasure.
You'll need to navigate the rooms and check items to see where they've hidden it.
When you find it, move it here, to the guard_room - and rename it "treasure".
If you do see any of those nasty goblins, remove them!

When you're done, try running the ./check_victory.sh program to see your progress!

Commands:

- cd room          - change to a new room
- cd ..            - go back a room
- ls               - show all the items and connected rooms in the current room
- pwd              - print where you are right now
- cat item         - prints the contents of an item. Use this to look for the treasure inside items.
- mv item location - moves an item to a new location. Use this to move the treasure to the guard_room and rename it.
- rm item          - removes an item. Use this only on Goblins! If you remove other things, you may lose points!
- ./check_victory  - only works in the guard_room. This will print your current score.

---
