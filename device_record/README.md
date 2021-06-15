# Device Record Python (version 2.7.X)

## Preparations: 
1. apt install xml2-dev gnutls-dev liblzma-dev libbz2-dev libasound-dev
 
## Run The Tool
1. start
```python
   python device_record.py
   or
   nohup python device_record.py &
```
   **Log file: /tmp/drecord.log. Audio File will be saved "/tmp/".** </br>
2. stop
```python
   ps -ef | grep device_record.py | grep -v grep | awk '{print $2}'| xargs kill -9
```
