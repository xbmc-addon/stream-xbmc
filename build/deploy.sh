cd `dirname $0`/../src

OLD=`cat ./setup.py | grep '__version__ =' | grep -E -o '[0-9\.]+'`
echo "Old version: $OLD"
echo -n 'New version: '
read NEW

sed -e "s/__version__ = \"$OLD\"/__version__ = \"$NEW\"/g" ./setup.py > ./setup2.py
mv ./setup2.py ./setup.py

cd ../build
./build.sh pro $NEW
