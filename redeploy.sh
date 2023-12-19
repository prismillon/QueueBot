#!/bin/bash
docker build --tag mogi-queuebot .
docker stop mogi-queuebot
docker rm mogi-queuebot
docker run -d --name mogi-queuebot --restart unless-stopped mogi-queuebot