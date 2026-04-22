#!/bin/sh
if [ -d "cli_dungeon" ]; then
	echo "Looks like the dungeon is already installed!"
	read -p "Do you want to reinstall? [Y/n]" reinstall
	if [ $reinstall = "n" ]; then
		echo "Bailing out."
		exit 1
	fi
	rm -rf cli_dungeon
fi
REPOSITORY_URL="https://raw.githubusercontent.com/paralinguist/CLI-Dungeon/main"
create_item()
{
 ITEM=`shuf -n 1 $HOME/items`
 echo contents: > $ITEM
 `shuf -n 1 $HOME/contents >> $ITEM`
}

create_room()
{
 mkdir $1
 cd $1
 ITEMS=$(shuf -i 0-3 -n 1)
 for i in {0..$ITEMS};
 do
  create_item
 done
 if [ $STAGE -eq $TREASURE ]; then
  ITEM=`shuf -n 1 $HOME/items`
  echo TREASURE! > $ITEM
  echo $TREASURE_CODE >> $ITEM 
 fi
 STAGE=$(($STAGE+1))
 GOBLIN=$(shuf -i 1-3 -n 1)
 if [ $GOBLIN -eq 3 ]; then
  echo GRARRR! > goblin.txt
 fi
 cd ..
}

STAGE=1
MAX_STAGES=8
TREASURE=$(shuf -i 3-$MAX_STAGES -n 1)
TREASURE_CODE=`hexdump -vn16 -e'4/4 "%08X" 1 "\n"' /dev/urandom`
TREASURE_HASH=`echo $TREASURE_CODE | md5sum`

mkdir cli_dungeon
cd cli_dungeon
mkdir guard_room
cd guard_room
curl -LJOs $REPOSITORY_URL/guard_room/check_victory.sh
chmod +x check_victory.sh
curl -LJOs $REPOSITORY_URL/guard_room/guard.txt
cd ..
curl -LJOs $REPOSITORY_URL/connectors 
curl -LJOs $REPOSITORY_URL/contents
curl -LJOs $REPOSITORY_URL/directions 
curl -LJOs $REPOSITORY_URL/items 
curl -LJOs $REPOSITORY_URL/rooms
shuf -n 2 directions > chosen_directions
HOME=`pwd`
FIRST_DIR=`head -1 chosen_directions`
SECOND_DIR=`tail -1 chosen_directions`
rm chosen_directions
shuf -n 3 rooms > first_rooms
shuf -n 1 connectors > first_connector
FIRST_ROOM1=`head -1 first_rooms`
FIRST_ROOM2=`head -2 first_rooms | tail -1`
FIRST_ROOM3=`tail -1 first_rooms`
FIRST_CONNECTOR=`cat first_connector`
rm first_rooms
rm first_connector
mkdir $FIRST_DIR
cd $FIRST_DIR
create_room $FIRST_ROOM1
create_room $FIRST_ROOM2
cd $FIRST_ROOM1
create_room $FIRST_CONNECTOR
cd $FIRST_CONNECTOR
create_room $FIRST_ROOM3
cd $HOME
shuf -n 3 rooms > second_rooms
shuf -n 1 connectors > second_connector
SECOND_ROOM1=`head -1 second_rooms`
SECOND_ROOM2=`head -2 second_rooms | tail -1`
SECOND_ROOM3=`tail -1 second_rooms`
SECOND_CONNECTOR=`cat second_connector`
rm second_rooms
rm second_connector
mkdir $SECOND_DIR
cd $SECOND_DIR
create_room $SECOND_ROOM1
create_room $SECOND_ROOM2
cd $SECOND_ROOM1
create_room $SECOND_CONNECTOR
cd $SECOND_CONNECTOR
create_room $SECOND_ROOM3
cd $HOME
cd guard_room
echo $TREASURE_HASH > signature
cd $HOME
rm directions
rm connectors
rm contents
rm items
rm rooms
echo Welcome to the CLI Dungeon!
echo You are the Command Wizard, you have been called in to help a Guard House which has been attacked by goblins.
echo Start by changing directory to the guard_room and using cat on the guard.txt to speak with them.
echo
echo "(Hint: type \"cd guard_room\" and hit enter, then type \"cat guard.txt\" and hit enter)"
