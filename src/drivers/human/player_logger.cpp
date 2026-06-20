#include "player_logger.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
# include <winsock2.h>
typedef SOCKET PlayerSocket;
# define BAD_SOCKET INVALID_SOCKET
# define CLOSE_SOCKET closesocket
#else
# include <arpa/inet.h>
# include <netdb.h>
# include <sys/socket.h>
# include <unistd.h>
typedef int PlayerSocket;
# define BAD_SOCKET (-1)
# define CLOSE_SOCKET close
#endif
#define MAX_PLAYERS 10
struct LoggerState { FILE *file; PlayerSocket socket; sockaddr_in peer; double nextSample; double lastTime; double period; unsigned long sequence; int active; int socketOpen; };
static LoggerState states[MAX_PLAYERS];
static int networkReady = 0;
static const char *envValue(const char *n, const char *d) { const char *v=getenv(n); return v&&v[0]?v:d; }
static void closeState(LoggerState *s) { if(s->file){fflush(s->file);fclose(s->file);} if(s->socketOpen)CLOSE_SOCKET(s->socket); memset(s,0,sizeof(*s)); }
static void openState(LoggerState *s,int player) {
 closeState(s); s->active=1; s->lastTime=-1.0; double hz=atof(envValue("TORCS_PLAYER_LOG_HZ","20")); s->period=hz>0.0?1.0/hz:0.05;
 char path[1024];
#ifdef _WIN32
 snprintf(path,sizeof(path),"%s\\player-%d-%ld.csv",envValue("TORCS_PLAYER_LOG_DIR","."),player,(long)time(NULL));
#else
 snprintf(path,sizeof(path),"%s/player-%d-%ld.csv",envValue("TORCS_PLAYER_LOG_DIR","."),player,(long)time(NULL));
#endif
 s->file=fopen(path,"w"); if(s->file){fputs("seq,sim_time,player,lap,x,y,yaw,speed_x,speed_y,accel_x,accel_y,to_middle,steer,throttle,brake,clutch,gear,rpm,fuel,damage\n",s->file);fflush(s->file);}
 if(!networkReady){
#ifdef _WIN32
  WSADATA data; if(WSAStartup(MAKEWORD(2,2),&data)!=0)return;
#endif
  networkReady=1;
 }
 s->socket=socket(AF_INET,SOCK_DGRAM,0); if(s->socket==BAD_SOCKET)return; s->socketOpen=1; memset(&s->peer,0,sizeof(s->peer)); s->peer.sin_family=AF_INET; s->peer.sin_port=htons((unsigned short)atoi(envValue("TORCS_PLAYER_UDP_PORT","3101")));
 const char *host=envValue("TORCS_PLAYER_UDP_HOST","127.0.0.1"); s->peer.sin_addr.s_addr=inet_addr(host); if(s->peer.sin_addr.s_addr==INADDR_NONE){hostent *e=gethostbyname(host);if(!e||e->h_length<=0){CLOSE_SOCKET(s->socket);s->socketOpen=0;return;}memcpy(&s->peer.sin_addr,e->h_addr,e->h_length);}
}
void PlayerLoggerSample(int player,const tCarElt *car,const tSituation *sit) {
 if(player<1||player>MAX_PLAYERS||!car||!sit)return; LoggerState *s=&states[player-1]; if(!s->active||sit->currentTime<s->lastTime)openState(s,player); s->lastTime=sit->currentTime; if(sit->currentTime+1e-9<s->nextSample)return; s->nextSample=sit->currentTime+s->period;
 char p[2048]; int n=snprintf(p,sizeof(p),"%lu,%.6f,%d,%d,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%d,%.6f,%.6f,%d\n",s->sequence++,sit->currentTime,player,car->_laps,car->_pos_X,car->_pos_Y,car->_yaw,car->_speed_x,car->_speed_y,car->_accel_x,car->_accel_y,car->_trkPos.toMiddle,car->_steerCmd,car->_accelCmd,car->_brakeCmd,car->_clutchCmd,car->_gearCmd,car->_enginerpm,car->_fuel,car->_dammage); if(n<=0)return;
 if(s->file){fwrite(p,1,(size_t)n,s->file);if((s->sequence%20)==0)fflush(s->file);} if(s->socketOpen)sendto(s->socket,p,n,0,(const sockaddr*)&s->peer,sizeof(s->peer));
}
