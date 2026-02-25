# docker run -p 8001:8000 ortools-api
# docker run --rm -p 8001:8000 ortools-api
# docker run -d -p 8001:8000 --name ortools-api ortools-api
docker run -d \
  --restart unless-stopped \
  -p 8001:8000 \
  --name ortools-api \
  ortools-api
