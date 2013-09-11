rm -f ~/Library/Logs/deluge.log

cd /Applications/Deluge.app
./Contents/MacOS/Deluge -L debug -l ~/Library/Logs/deluge.log &
