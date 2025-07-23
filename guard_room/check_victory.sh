#!/bin/sh
POINTS=0
echo Checking treasure has been moved from the dungeon...
MOVED=`grep -R "TREASURE!" ../ --exclude-dir=guard_room`
if [ -z "${MOVED}" ]; then
 echo Excellent - the treasure has been moved!
 POINTS=$(($POINTS+5))
else
 echo Oh no! Looks like the treasure is still in the dungeon.
fi
TREASURE=""
HASH="A"
echo Checking treasure is in the guard room...
if [ -f treasure ]; then
 TREASURE=`tail -1 treasure | md5sum | awk '{print $1}'`
 HASH=`cat signature | awk '{print $1}'`
 if [ "$TREASURE" = "$HASH" ]; then
  echo Excellent, the treasure is in the guard room!
  POINTS=$(($POINTS+10))
 else
  echo Oh dear, no treasure here.
 fi
fi
echo Checking for goblins still in the dungeon...
GOBLINS=`find ../ -name "goblin.txt" | wc -l`
if [ $GOBLINS -eq 0 ]; then
 echo Great work, you removed all the goblins!
else
 echo Oh dear - there are still some goblins in the dungeon.
 echo Minus one point for each goblin left - you are down $GOBLINS points.
 POINTS=$(($POINTS-$GOBLINS)) 
fi
echo Your final score for this dungeon: $POINTS/15
