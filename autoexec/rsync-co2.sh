#!/bin/bash
#set -x

TOUCHFILE=~misura/00-RSYNC_IN_PROGRESS
trap "rm $TOUCHFILE && exit" SIGKILL SIGINT SIGABRT SIGTERM

if [ -e $TOUCHFILE ]
then 
        echo "CO2, rsync gia in corso"
        exit 1
fi

touch $TOUCHFILE
date > $TOUCHFILE
rsync -avz -e ssh /home/misura/data/ cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343
#rsync -avz -e ssh /home/misura/programmi cimone@ozone.bo.isac.cnr.it:/home/cimone/data/gmp343/
rm $TOUCHFILE
