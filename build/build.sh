cd `dirname $0`/..

BUILD=dev

if [ "$#" -eq "2" ]
then
  if [ "$1" = "pro" ]
  then
    BUILD=pro
  fi
fi

rm -rf ./temp
mkdir ./temp
cp -R ./src/* ./temp/

cd ./temp
python setup.py bdist_egg
cd ../

if [ "$BUILD" = "pro" ]
then
  rm -rf ./dist/*
  cp ./temp/dist/StreamXBMC-$2-py2.7.egg ./dist/
  cp ./temp/dist/StreamXBMC-$2-py2.7.egg ./dist/StreamXBMC-$2-py2.6.egg
else
  FILENAME=`ls ./temp/dist/`
  cp ./temp/dist/$FILENAME /Applications/Deluge.app/Contents/Resources/lib/python2.7/deluge-1.3.6-py2.7.egg/deluge/plugins/StreamXBMC-py2.7.egg
fi

rm -rf ./temp
