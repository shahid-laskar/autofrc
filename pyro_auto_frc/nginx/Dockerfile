FROM nginx:1.27-alpine

# Remove default config
RUN rm /etc/nginx/conf.d/default.conf

# Copy app config
COPY frc.conf /etc/nginx/conf.d/frc.conf

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]